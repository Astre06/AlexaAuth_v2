# ============================================================
# 🧹 Logging Configuration
# ============================================================
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import logging
import os
import re
import time
import json
import random
import string
import threading
import asyncio
import subprocess
import html
import shutil
from urllib.parse import urlparse
from datetime import datetime
import glob
from site_auth_manager import ensure_user_site_exists

# Silence noisy urllib3 logs
logging.getLogger("urllib3").setLevel(logging.WARNING)
from site_auth_manager import replace_user_sites
from mass_check import merge_livecc_user_files

# ============================================================
# 🧩 Telegram Bot Imports
# ============================================================
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException
import functools

# ============================================================
# ⚙️ Config & Global Settings
# ============================================================
from config import load_config
from site_auth_manager import ensure_user_site_exists
from runtime_config import set_default_site, get_default_site, RUNTIME_CONFIG
cfg = load_config()
BOT_TOKEN = cfg["BOT_TOKEN"]
CHANNEL_ID = cfg["CHANNEL_ID"]
ADMIN_ID = cfg["ADMIN_ID"]
MAX_WORKERS = cfg["MAX_WORKERS"]
BATCH_SIZE = cfg["BATCH_SIZE"]
DELAY_BETWEEN_BATCHES = cfg["DELAY_BETWEEN_BATCHES"]

from shared_state import save_live_cc_to_json
from proxy_manager import (
    add_user_proxy,
    replace_user_proxies,
    delete_user_proxies,
    list_user_proxies,
    get_user_proxy,
)

from mass_check import (
    handle_file as handle_mass_file,
    run_mass_check_thread,
    get_stop_event,
    set_stop_event,
    clear_stop_event,
    is_stop_requested,
    stop_events,
)

from manual_check import register_manual_check
from proxy_manager import parse_proxy_line
from proxy_check import register_checkproxy
from site_auth_manager import (
    SiteAuthManager,
    _load_state,
    _save_state,
    process_card_for_user_sites,
)

# ============================================================
# 🧩 Thread-safe utilities
# ============================================================
send_lock = threading.Lock()

def try_send(func, *args, **kwargs):
    """Non-blocking Telegram sender (logs, no retry)."""
    try:
        with send_lock:
            return func(*args, **kwargs)
    except ApiTelegramException as e:
        logging.warning(f"[TELEGRAM ERROR] {e}")
    except Exception as e:
        logging.warning(f"[SEND ERROR] {e}")
    return None

def send_async(bot, method_name, *args, **kwargs):
    """Run any bot.send_* in its own thread (never blocks main loop)."""
    def runner():
        try:
            fn = getattr(bot, method_name)
            try_send(fn, *args, **kwargs)
        except Exception as e:
            logging.debug(f"[ASYNC SEND ERROR] {e}")
    threading.Thread(target=runner, daemon=True).start()

def delete_after_delay(bot, chat_id, msg_id, delay=5.0):
    """Delete message after delay asynchronously."""
    def deleter():
        try_send(bot.delete_message, chat_id, msg_id)
    threading.Timer(delay, deleter).start()

# ============================================================
# 🧩 Global dictionaries and state
# ============================================================
user_sites = {}
from shared_state import user_busy
from mass_check import activechecks
from manual_check import user_locks, user_locks_lock

# ============================================================
# 🚦 USER BUSY TRACKER (thread-safe)
# ============================================================
def is_user_busy(chat_id: str):
    """Return True if user currently has an active mass or manual check."""
    if user_busy.get(chat_id):
        return True
    if chat_id in activechecks:
        return True
    with user_locks_lock:
        if chat_id in user_locks and user_locks[chat_id].locked():
            return True
    return False

# ============================================================
# 🧩 Safe state loader
# ============================================================
def safe_load_state(chat_id):
    try:
        return _load_state(chat_id)
    except Exception as e:
        logging.warning(f"[WARN] Failed to load state for {chat_id}: {e}")
        return {}

# ============================================================
# 🧩 AUTO-DEFAULT SITE INITIALIZER
# ============================================================
def ensure_user_default_site(chat_id):
    """
    Ensures that a per-user sites_<id>.json exists inside /sites/<chat_id>/,
    with proper nested structure and default site from runtime_config.
    """
    try:
        default_site = get_default_site()
        user_dir = os.path.join("sites", str(chat_id))
        os.makedirs(user_dir, exist_ok=True)
        file_path = os.path.join(user_dir, f"sites_{chat_id}.json")

        if not os.path.exists(file_path):
            default_state = {
                str(chat_id): {
                    "sites": {
                        default_site: {
                            "accounts": [],
                            "cookies": None,
                            "payment_count": 0,
                            "mode": "rotate",
                        }
                    }
                }
            }
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(default_state, f, indent=2)
            logging.info(f"[AUTO-SITE] Created default site file for {chat_id}")
        else:
            logging.debug(f"[AUTO-SITE] Site file already exists for {chat_id}")
    except Exception as e:
        logging.error(f"[AUTO-SITE ERROR] {chat_id}: {e}")

from sitechk import check_command, get_base_url
from bininfo import round_robin_bin_lookup

# ============================================================
# 💳 Card Generator (Optional Utilities)
# ============================================================
from cardgen import (
    generate_luhn_cards_parallel,
    generate_luhn_cards_fixed_expiry,
    save_cards_to_file,
    get_random_expiry,
)

# ============================================================
# 🤖 INITIALIZE BOT
# ============================================================
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ============================================================
# 🧩 Global Safe Forward Patch — Logs channel errors silently
# ============================================================
_original_send_message = bot.send_message
_original_send_document = bot.send_document
_original_send_photo = bot.send_photo
_original_send_video = bot.send_video

def _safe_wrapper(func_name, orig_func):
    @functools.wraps(orig_func)
    def wrapped(*args, **kwargs):
        try:
            return orig_func(*args, **kwargs)
        except Exception as e:
            if len(args) > 0 and str(args[0]) == str(CHANNEL_ID):
                logging.debug(f"[CHANNEL_FORWARD_ERROR:{func_name}] {e}")
            else:
                logging.warning(f"[SEND ERROR:{func_name}] {e}")
    return wrapped

bot.send_message = _safe_wrapper("send_message", _original_send_message)
bot.send_document = _safe_wrapper("send_document", _original_send_document)
bot.send_photo = _safe_wrapper("send_photo", _original_send_photo)
bot.send_video = _safe_wrapper("send_video", _original_send_video)



# ============================================================
# 🧩 Safe Telegram Sender (Non-Blocking + Thread-Safe)
# ============================================================
def safe_send(bot, method, *args, **kwargs):
    """
    Thread-safe Telegram sender that runs in a separate thread.
    Avoids blocking the polling loop or main threads.
    """

    def run():
        try:
            fn = getattr(bot, method)
            try:
                fn(*args, **kwargs)
            except telebot.apihelper.ApiTelegramException as e:
                err_text = str(e)
                if "Too Many Requests" in err_text:
                    import re
                    match = re.search(r"retry after (\d+)", err_text)
                    wait = int(match.group(1)) if match else 5
                    logging.warning(f"[RATE-LIMIT] Retry after {wait}s → {method}")
                    time.sleep(wait)
                    try:
                        fn(*args, **kwargs)
                    except Exception as e2:
                        logging.error(f"[safe_send RETRY FAIL] {e2}")
                else:
                    logging.warning(f"[safe_send TELEGRAM ERROR] {e}")
            except Exception as e:
                logging.warning(f"[safe_send GENERAL ERROR] {e}")
        except Exception as e:
            logging.error(f"[safe_send FATAL] {e}")

    threading.Thread(target=run, daemon=True).start()


# ============================================================
# 🧩 Imports for Utility Modules
# ============================================================
from cardgen import (
    generate_luhn_cards_parallel,
    generate_luhn_cards_fixed_expiry,
    save_cards_to_file,
    get_random_expiry,
)

from bininfo import round_robin_bin_lookup
from sitechk import get_base_url
from proxy_manager import parse_proxy_line
from manual_check import register_manual_check
from mass_check import handle_file

# -------------------------------------------------
# Base Directory
# -------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# -------------------------------------------------
# Logging Configuration
# -------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)

# -------------------------------------------------
# File Constants
# -------------------------------------------------
SITE_STORAGE_FILE = "current_site.txt"
ALLOWED_USERS_FILE = "allowed_users.json"
MASTER_FILE = "master_live_ccs.json"
REDEEM_CODES_FILE = "redeem_codes.json"

# Proxy state
user_proxy_mode = set()

# ============================================================
# 🔒 Command Control System — prevents overlapping commands
# ============================================================
user_active_command = {}
command_lock = threading.Lock()


def set_active_command(chat_id, command):
    """Register a new command for this user and cancel previous one."""
    with command_lock:
        user_active_command[chat_id] = command


def clear_active_command(chat_id):
    """Clear the user's active command."""
    with command_lock:
        user_active_command.pop(chat_id, None)


def is_command_active(chat_id, command=None):
    """Check if a user currently has an active command."""
    with command_lock:
        if command:
            return user_active_command.get(chat_id) == command
        return chat_id in user_active_command


def reset_user_states(chat_id):
    """Clear all temp variables from /site or /proxy setup safely."""
    for var_dict in ("user_site_last_instruction", "user_proxy_temp", "user_proxy_messages"):
        try:
            globals()[var_dict].pop(chat_id, None)
        except Exception:
            pass


# ============================================================
# 🧹 Auto-Delete Message Helper (Non-Blocking)
# ============================================================
def _auto_delete_message_later(bot, chat_id, message_id, delay=5):
    """
    Deletes a Telegram message after a specified delay (non-blocking).
    Used for temporary info or error messages.
    """

    def delete_later():
        try:
            time.sleep(delay)
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass

    threading.Thread(target=delete_later, daemon=True).start()


# ✅ Register Proxy Checker
register_checkproxy(bot)

# ============================================================
# 🧩 Helpers: JSON Persistence
# ============================================================
ALLOWED_FILE = "allowed_users.json"
allowed_lock = threading.Lock()


def load_allowed_users():
    """Thread-safe loader for allowed_users list."""
    with allowed_lock:
        if os.path.exists(ALLOWED_FILE):
            try:
                with open(ALLOWED_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # normalize data
                    if isinstance(data, dict):
                        data = list(data.keys())
                    elif not isinstance(data, list):
                        data = []
                    logging.info(f"[LOAD] {len(data)} allowed users loaded")
                    return data
            except Exception as e:
                logging.error(f"[LOAD ERROR] allowed_users: {e}")
                return []
        else:
            logging.warning("[LOAD WARN] allowed_users.json not found")
            return []


# -------------------------------------------------
# Initialize or load existing allowed_users.json
# -------------------------------------------------
if os.path.exists(ALLOWED_FILE):
    try:
        with open(ALLOWED_FILE, "r", encoding="utf-8") as f:
            allowed_users = json.load(f)
        if not isinstance(allowed_users, list):
            logging.warning("[INIT WARN] allowed_users.json not a list → reset empty")
            allowed_users = []
    except Exception as e:
        logging.error(f"[INIT ERROR] Could not read allowed_users.json: {e}")
        allowed_users = []
else:
    logging.info("[INIT] allowed_users.json not found → creating empty list")
    allowed_users = []


def save_allowed_users(data):
    """Thread-safe saver for allowed_users list."""
    with allowed_lock:
        try:
            with open(ALLOWED_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
            logging.info(f"[SAVE] Allowed users saved ({len(data)})")
        except Exception as e:
            logging.error(f"[SAVE ERROR] allowed_users: {e}")


# Initialize
allowed_users = load_allowed_users()
from config import ADMIN_ID

# Auto-add admin if missing
if str(ADMIN_ID) not in allowed_users:
    allowed_users.append(str(ADMIN_ID))
    save_allowed_users(allowed_users)
    print(f"[INFO] Admin {ADMIN_ID} auto-added to allowed users.")


# ============================================================
# 💾 User Live CC JSON Utilities
# ============================================================
def load_user_live_ccs(chat_id):
    path = f"live_ccs_{chat_id}.json"
    if not os.path.exists(path):
        logging.debug(f"[LIVECC LOAD] No file for {chat_id}")
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        logging.debug(f"[LIVECC LOAD] {len(data)} entries for {chat_id}")
        return data
    except Exception as e:
        logging.warning(f"[LIVECC LOAD ERROR] {e}")
        return []


def save_user_live_ccs(chat_id, ccs):
    path = f"live_ccs_{chat_id}.json"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ccs, f, indent=2)
        logging.debug(f"[LIVECC SAVE] {len(ccs)} entries for {chat_id}")
    except Exception as e:
        logging.error(f"[LIVECC SAVE ERROR] {e}")


def load_master_live_ccs():
    if not os.path.exists(MASTER_FILE):
        with open(MASTER_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)
        logging.debug("[MASTER LIVECC] Created empty file")
        return []
    try:
        with open(MASTER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        logging.debug(f"[MASTER LIVECC] Loaded {len(data)} entries")
        return data
    except Exception as e:
        logging.warning(f"[MASTER LIVECC LOAD ERROR] {e}")
        return []


def save_master_live_ccs(ccs):
    try:
        with open(MASTER_FILE, "w", encoding="utf-8") as f:
            json.dump(ccs, f, indent=2)
        logging.debug(f"[MASTER LIVECC SAVE] {len(ccs)} entries")
    except Exception as e:
        logging.error(f"[MASTER LIVECC SAVE ERROR] {e}")


def load_redeem_codes():
    if not os.path.exists(REDEEM_CODES_FILE):
        with open(REDEEM_CODES_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)
        logging.debug("[REDEEM] Created empty file")
        return []
    try:
        with open(REDEEM_CODES_FILE, "r", encoding="utf-8") as f:
            codes = json.load(f)
        logging.debug(f"[REDEEM] Loaded {len(codes)} codes")
        return codes
    except Exception as e:
        logging.warning(f"[REDEEM LOAD ERROR] {e}")
        return []


def save_redeem_codes(codes):
    try:
        with open(REDEEM_CODES_FILE, "w", encoding="utf-8") as f:
            json.dump(codes, f, indent=2)
        logging.debug(f"[REDEEM] Saved {len(codes)} codes")
    except Exception as e:
        logging.error(f"[REDEEM SAVE ERROR] {e}")


# -------------------------------------------------
# Global State
# -------------------------------------------------
valid_redeem_codes = load_redeem_codes()
register_manual_check(bot, allowed_users)

# -------------------------------------------------
# Utility Functions
# -------------------------------------------------
def generate_redeem_code():
    return "-".join(
        "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
        for _ in range(3)
    )


site_last_instruction = {}


def save_current_site(sites):
    """Save current active sites, removing duplicates but preserving order."""
    unique_sites = []
    for s in sites:
        if s not in unique_sites:
            unique_sites.append(s)
    try:
        with open(SITE_STORAGE_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(unique_sites) + "\n")
        logging.debug(f"[SITE SAVE] {len(unique_sites)} sites")
    except Exception as e:
        logging.error(f"[SITE SAVE ERROR] {e}")
    return unique_sites


def load_current_site():
    """Load the last used site list from file or return default."""
    try:
        with open(SITE_STORAGE_FILE, "r", encoding="utf-8") as f:
            sites = [line.strip() for line in f if line.strip()]
            return sites if sites else [DEFAULT_API_URL]
    except FileNotFoundError:
        return [DEFAULT_API_URL]
    except Exception as e:
        logging.warning(f"[SITE LOAD ERROR] {e}")
        return [DEFAULT_API_URL]


# ============================================================
# 📖 Start & Help Commands
# ============================================================
COMMAND_HELP = {
    "gen": ("/gen", "/gen <BIN> <MM|YY>\nGenerate 10 sample test cards (example: /gen 478200 11|25)."),
    "gens": ("/gens", "/gens <BIN> <MM|YY> <count>\nBulk generate cards (saves as .txt)."),
    "chk": ("/chk or .chk", "/chk <card>\nCheck a single card. Format: number|mm|yyyy|cvc."),
    "check": ("/check", "/check <url> [card]\nAnalyze a site's payment gateway."),
    "mass": ("/mass", "/mass\nUpload a .txt file with cards to run a mass check."),
    "site": ("/site", "/site\nManage your saved sites."),
    "sitelist": ("/sitelist", "/sitelist\nShow your saved sites."),
    "proxy": ("/proxy", "/proxy\nManage your proxies."),
    "checkproxy": ("/checkproxy", "/checkproxy <proxy>\nTest a single proxy."),
    "request": ("/request", "/request\nSend an access request to the admin."),
    "clean": ("/clean", "/clean\nMake the file only cards."),
}


@bot.message_handler(commands=["start"])
def handle_start(message):
    chat_id = str(message.chat.id)
    username = (message.from_user.username or message.from_user.first_name or "User")

    # ✅ Ensure per-user environment exists
    ensure_user_site_exists(chat_id)

    try:
        user_folder = os.path.join("live-cc", chat_id)
        os.makedirs(user_folder, exist_ok=True)

        base_json = os.path.join(user_folder, f"Live_cc_{chat_id}_1.json")
        if not os.path.exists(base_json):
            with open(base_json, "w", encoding="utf-8") as f:
                f.write("[]")
        else:
            with open(base_json, "r+", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    f.seek(0)
                    f.write("[]")
                    f.truncate()
    except Exception as e:
        logging.warning(f"[START ERROR] Could not create live folder for {chat_id}: {e}")

    # 🔹 If not allowed → minimal keyboard
    if chat_id not in allowed_users:
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("Request access /request", callback_data="usage_request"))
        threading.Thread(
            target=lambda: safe_send(
                bot,
                "send_message",
                chat_id,
                f"Hello <b>{username}</b> — you are not authorized yet.\n"
                "Press the button below to see how to request access.",
                parse_mode="HTML",
                reply_markup=kb,
            ),
            daemon=True,
        ).start()
        return

    # 🔹 Authorized user keyboard
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("Generate (/gen)", callback_data="usage_gen"),
        types.InlineKeyboardButton("Bulk gen (/gens)", callback_data="usage_gens"),
        types.InlineKeyboardButton("Single check (/chk)", callback_data="usage_chk"),
        types.InlineKeyboardButton("Site check (/check)", callback_data="usage_check"),
        types.InlineKeyboardButton("Mass (/mass)", callback_data="usage_mass"),
        types.InlineKeyboardButton("Sites (/site)", callback_data="usage_site"),
        types.InlineKeyboardButton("Sitelist (/sitelist)", callback_data="usage_sitelist"),
        types.InlineKeyboardButton("Proxy (/proxy)", callback_data="usage_proxy"),
        types.InlineKeyboardButton("Check Proxy (/checkproxy)", callback_data="usage_checkproxy"),
        types.InlineKeyboardButton("Clean (/clean)", callback_data="usage_clean"),
    )

    safe_send(
        bot,
        "send_message",
        chat_id,
        f"Hello, <b>{username}</b>.\nTap any command below to see its usage example.",
        parse_mode="HTML",
        reply_markup=kb,
    )



# -------------------------------------------------------------
# Callback handler for the buttons above (usage messages)
# -------------------------------------------------------------
@bot.callback_query_handler(func=lambda call: str(call.data).startswith("usage_"))
def handle_usage_button(call):
    try:
        data = call.data  # e.g. "usage_gen"
        parts = data.split("_", 1)
        if len(parts) != 2:
            safe_send(bot, "answer_callback_query", call.id, text="Invalid request.")
            return

        cmd = parts[1]
        label, help_text = COMMAND_HELP.get(
            cmd, (cmd, "No usage info available for this command.")
        )

        # Acknowledge press (tooltip)
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass

        import html
        safe_send(
            bot,
            "send_message",
            call.message.chat.id,
            f"<b>{label}</b>\n\n<code>{html.escape(help_text)}</code>",
            parse_mode="HTML",
        )

    except Exception as e:
        logging.error(f"[USAGE BUTTON ERROR] {e}")
        try:
            bot.answer_callback_query(call.id, "Error retrieving usage.")
        except Exception:
            pass


# ============================================================
# Admin Command / Help
# ============================================================
@bot.message_handler(commands=["help", "cmd", "cmds", "cmnds"])
def show_commands(message):
    chat_id = str(message.chat.id)
    if chat_id == str(ADMIN_ID):
        # 👑 Full admin command list
        msg = (
            "🤖 <b>Admin Commands</b>\n\n"
            "/chk <code>card|mm|yy|cvc</code> – Check single card\n"
            "/site – Manage sites (Admin)\n"
            "/sitelist – Show sites (Admin)\n"
            "/proxy – Manage proxies (Admin)\n"
            "/get all – Get all live CCs (from all users)\n"
            "/get all <code>USER_ID</code> – Get lives from specific user\n"
            "/get all bin <code>BIN</code> – Filter by BIN (all files)\n"
            "/get all bank <code>BANK</code> – Filter by bank (all files)\n"
            "/get all country <code>COUNTRY</code> – Filter by country (all files)\n"
            "/get_master_data – Admin only\n"
            "/code – Generate redeem code\n"
            "/redeem <code>CODE</code> – Redeem access code\n"
            "/request – Request access\n"
            "/send <code>MESSAGE</code> – Broadcast message\n"
            "/delete <code>USER_ID</code> – Remove user\n"
        )
        safe_send(bot, "send_message", chat_id, msg, parse_mode="HTML")
        logging.debug(f"/help full command list shown to admin {chat_id}")
    else:
        short_msg = "✅ Just send .txt file with cards.\n\nFor more commands, use /start."
        safe_send(bot, "send_message", chat_id, short_msg, parse_mode="HTML")
        logging.debug(f"/help short help shown to user {chat_id}")


# ============================================================
# /botdel — Delete an existing sub-bot folder
# ============================================================
@bot.message_handler(commands=["botdel"])
def delete_bot_folder(message):
    chat_id = str(message.chat.id)
    if chat_id != str(ADMIN_ID):
        safe_send(bot, "reply_to", message, "🚫 Admin only.")
        return

    args = message.text.strip().split()
    if len(args) != 2:
        safe_send(bot, "reply_to", message, "Usage: /botdel <USER_ID>")
        return

    user_id = args[1]
    folder = os.path.join(os.getcwd(), "bots", user_id)
    try:
        if os.path.exists(folder):
            shutil.rmtree(folder)
            safe_send(bot, "reply_to", message, f"Bot for {user_id} deleted successfully.")
        else:
            safe_send(bot, "reply_to", message, f"⚠️ No bot found for {user_id}.")
    except Exception as e:
        safe_send(bot, "reply_to", message, f"❌ Error deleting bot: {e}")


# ============================================================
# Admin User Management
# ============================================================
@bot.message_handler(commands=["add"])
def add_user(message):
    chat_id = str(message.chat.id)
    if chat_id != str(ADMIN_ID):
        safe_send(bot, "reply_to", message, "🚫 Admin only")
        return

    args = message.text.split()
    if len(args) < 2:
        safe_send(bot, "reply_to", message, "Usage: /add USER_ID")
        return

    new_user_id = args[1]
    if new_user_id not in allowed_users:
        allowed_users.append(new_user_id)
        save_allowed_users(allowed_users)
        safe_send(bot, "reply_to", message, f"✅ User {new_user_id} added successfully")
        logging.info(f"Added new user: {new_user_id}")
    else:
        safe_send(bot, "reply_to", message, f"⚠️ User {new_user_id} already exists")


@bot.message_handler(commands=["delete", "del"])
def delete_user(message):
    chat_id = str(message.chat.id)
    if chat_id != str(ADMIN_ID):
        safe_send(bot, "reply_to", message, "🚫 Admin only")
        return

    args = message.text.split()
    if len(args) < 2:
        safe_send(bot, "reply_to", message, "Usage: /delete USER_ID")
        return

    user_id_to_delete = args[1]
    if user_id_to_delete in allowed_users:
        allowed_users.remove(user_id_to_delete)
        save_allowed_users(allowed_users)
        try:
            path = f"live_ccs_{user_id_to_delete}.json"
            if os.path.exists(path):
                os.remove(path)
        except Exception as e:
            logging.warning(f"[DELETE FILE ERROR] {e}")
        safe_send(bot, "reply_to", message, f"✅ User {user_id_to_delete} removed")
        logging.info(f"Deleted user: {user_id_to_delete}")
    else:
        safe_send(bot, "reply_to", message, "⚠️ User not found")


# ============================================================
# Redeem Codes
# ============================================================
@bot.message_handler(commands=["code"])
def generate_code(message):
    chat_id = str(message.chat.id)
    if chat_id != str(ADMIN_ID):
        safe_send(bot, "reply_to", message, "🚫 Admin only")
        return

    new_code = generate_redeem_code()
    valid_redeem_codes.append(new_code)
    save_redeem_codes(valid_redeem_codes)

    safe_send(
        bot,
        "reply_to",
        message,
        f"<b>🎉 New Redeem Code 🎉</b>\n\n<code>{new_code}</code>",
        parse_mode="HTML",
    )
    logging.info(f"[CODE GEN] New redeem code: {new_code}")


@bot.message_handler(commands=["redeem"])
def redeem_code(message):
    chat_id = str(message.chat.id)
    args = message.text.split()
    if len(args) < 2:
        safe_send(bot, "reply_to", message, "Usage: /redeem CODE")
        return

    code = args[1]
    if code in valid_redeem_codes:
        if chat_id not in allowed_users:
            allowed_users.append(chat_id)
            save_allowed_users(allowed_users)
            valid_redeem_codes.remove(code)
            save_redeem_codes(valid_redeem_codes)
            safe_send(bot, "reply_to", message, "✅ Access granted!")
            logging.info(f"[REDEEM] {chat_id} redeemed {code}")
        else:
            safe_send(bot, "reply_to", message, "⚠️ You already have access")
    else:
        safe_send(bot, "reply_to", message, "❌ Invalid code")


# ================================================================
# /request — user access request with Approve / Decline buttons
# ================================================================
@bot.message_handler(commands=["request"])
def handle_request(message):
    chat_id = str(message.chat.id)
    user = message.from_user

    if chat_id in allowed_users:
        safe_send(bot, "reply_to", message, "✅ You already have access. Use /start to continue.")
        return

    username = f"@{user.username}" if user.username else user.first_name or "User"
    user_info = f"👤 <b>{username}</b>\n🆔 <code>{chat_id}</code>"

    safe_send(bot, "reply_to", message, "⌛ Your access request has been sent to the admin.")

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ Approve", callback_data=f"approve_{chat_id}"),
        types.InlineKeyboardButton("❌ Decline", callback_data=f"decline_{chat_id}")
    )

    text = f"📨 <b>New Access Request</b>\n\n{user_info}"
    try:
        safe_send(bot, "send_message", ADMIN_ID, text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logging.error(f"[REQUEST ERROR] {e}")


# ================================================================
# /gen Preview Builder
# ================================================================
def build_gen_preview_html(bin_prefix, expiry, lines, bin_info, username_display):
    """Build HTML preview for /gen output."""
    def escape_html(x): return html.escape(str(x))
    lines_preview = "\n".join(f"<code>{escape_html(l)}</code>" for l in lines)
    flag = bin_info.get("country_flag", "")
    bank = escape_html(bin_info.get("bank", "Unknown Bank"))
    display = escape_html(bin_info.get("display_clean", "Unknown"))
    country = escape_html(bin_info.get("country", "Unknown Country"))

    html_content = (
        "<b>✅ Card Generated Successfully ✅</b>\n\n"
        f"<b>BIN →</b> <code>{escape_html(bin_info.get('bin'))}</code>\n"
        f"<b>Amount →</b> {len(lines)}\n"
        f"<b>Expiry →</b> {escape_html(expiry)}\n\n"
        f"{lines_preview}\n\n"
        f"<b>Info:</b> {display}\n"
        f"<b>Issuer:</b> {bank}\n"
        f"<b>Country:</b> {country} {flag}\n\n"
        f"<b>Generated By:</b> {escape_html(username_display)}"
    )
    return html_content


# ================================================================
# /gen command — Always 10 valid cards + correct BIN info
# ================================================================
@bot.message_handler(commands=["gen"])
def handle_gen(message):
    chat_id = str(message.chat.id)
    set_active_command(chat_id, "gen")
    reset_user_states(chat_id)

    if chat_id not in allowed_users:
        safe_send(bot, "reply_to", message,
                  "🚫 You don't have access.\nUse /request to ask the admin.")
        return

    text = message.text.replace("|", " ").strip()
    parts = text.split()

    if len(parts) == 2:
        _, bin_prefix = parts
        expiry_text = "Random per card"
        use_random_expiry = True
        mm = yy = None
    elif len(parts) >= 4:
        _, bin_prefix, mm, yy = parts[:4]
        expiry_text = f"{mm}|{yy}"
        use_random_expiry = False
    else:
        msg = safe_send(
            bot,
            "reply_to",
            message,
            "❌ Usage: /gen [BIN] [MM YY]\nExample: /gen 123456 12 29\nOr: /gen 123456 (random expiry)",
        )
        if msg and hasattr(msg, "message_id"):
            _auto_delete_message_later(bot, chat_id, msg.message_id, delay=6)
        return

    try:
        cards, max_retries = [], 15
        while len(cards) < 10 and max_retries > 0:
            needed = 10 - len(cards)
            new_cards = (
                generate_luhn_cards_parallel(bin_prefix, needed)
                if use_random_expiry
                else generate_luhn_cards_fixed_expiry(bin_prefix, mm, yy, needed)
            )
            cards.extend(new_cards)
            max_retries -= 1
        cards = cards[:10]
        if len(cards) < 10:
            raise RuntimeError(f"Only generated {len(cards)} cards after retries.")
    except Exception as e:
        msg = safe_send(bot, "reply_to", message, f"⚠️ Error generating cards: {e}")
        if msg and hasattr(msg, "message_id"):
            _auto_delete_message_later(bot, chat_id, msg.message_id, delay=6)
        clear_active_command(chat_id)
        return

    try:
        bin_info = round_robin_bin_lookup(bin_prefix)
    except Exception as e:
        logging.warning(f"[BIN LOOKUP FAIL] {e}")
        bin_info = {
            "bin": bin_prefix[:6],
            "display_clean": "Unknown",
            "bank": "Unknown Bank",
            "country": "Unknown Country",
            "country_flag": "",
        }

    try:
        user = bot.get_chat(chat_id)
        username_display = f"@{user.username}" if user.username else user.first_name or f"User {chat_id}"
    except Exception:
        username_display = f"User {chat_id}"

    html_preview = build_gen_preview_html(bin_prefix, expiry_text, cards, bin_info, username_display)

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    cb_data = f"regen|{bin_prefix}|{'RANDOM' if use_random_expiry else expiry_text}"
    keyboard.add(types.InlineKeyboardButton("🎲 Regenerate CC", callback_data=cb_data))

    safe_send(bot, "send_message", chat_id, html_preview, parse_mode="HTML", reply_markup=keyboard)
    clear_active_command(chat_id)


# ================================================================
# /regen callback handler — Always 10 valid cards + correct BIN info
# ================================================================
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("regen|"))
def handle_regenerate_callback(call):
    try:
        _, bin_prefix, expiry = call.data.split("|", 2)
        cards, max_retries = [], 15
        use_random_expiry = expiry == "RANDOM"

        if not use_random_expiry:
            mm, yy = expiry.split("|")

        while len(cards) < 10 and max_retries > 0:
            needed = 10 - len(cards)
            new_cards = (
                generate_luhn_cards_parallel(bin_prefix, needed)
                if use_random_expiry
                else generate_luhn_cards_fixed_expiry(bin_prefix, mm, yy, needed)
            )
            cards.extend(new_cards)
            max_retries -= 1
        cards = cards[:10]
        if len(cards) < 10:
            raise RuntimeError(f"Only generated {len(cards)} cards after retries.")

        try:
            bin_info = round_robin_bin_lookup(bin_prefix)
        except Exception as e:
            logging.warning(f"[BIN LOOKUP FAIL: REGEN] {e}")
            bin_info = {
                "bin": bin_prefix[:6],
                "display_clean": "Unknown",
                "bank": "Unknown Bank",
                "country": "Unknown Country",
                "country_flag": "",
            }

        try:
            user = bot.get_chat(call.from_user.id)
            username_display = f"@{user.username}" if user.username else user.first_name or f"User {call.from_user.id}"
        except Exception:
            username_display = f"User {call.from_user.id}"

        display_expiry = "Random per card" if use_random_expiry else expiry
        new_html = build_gen_preview_html(bin_prefix, display_expiry, cards, bin_info, username_display)

        safe_send(
            bot,
            "edit_message_text",
            new_html,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="HTML",
            reply_markup=call.message.reply_markup,
        )
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
    except Exception as e:
        safe_send(bot, "answer_callback_query", call.id, text=f"⚠️ Error: {e}", show_alert=True)


# ================================================================
# /gens — Bulk generate BIN cards safely in background thread
# ================================================================
@bot.message_handler(commands=["gens"])
def handle_gens(message):
    chat_id = str(message.chat.id)
    set_active_command(chat_id, "gens")
    reset_user_states(chat_id)

    if chat_id not in allowed_users:
        safe_send(bot, "reply_to", message,
                  "🚫 You don't have access to this command.\nUse /request to ask the admin.")
        return

    text = message.text.replace("|", " ").strip()
    parts = text.split()

    if len(parts) == 3:
        _, bin_prefix, count_str = parts
        use_random_expiry = True
        mm, yy = None, None
    elif len(parts) == 5:
        _, bin_prefix, mm, yy, count_str = parts
        use_random_expiry = False
    else:
        msg = safe_send(
            bot,
            "reply_to",
            message,
            "❌ Usage: /gens [BIN] [COUNT]\n   or /gens [BIN] [MM YY] [COUNT]\n"
            "Example:\n/gens 123456 100\n/gens 123456 12 29 100",
        )
        if msg and hasattr(msg, "message_id"):
            _auto_delete_message_later(bot, chat_id, msg.message_id, delay=6)
        return

    try:
        count = int(count_str)
        if count <= 0 or count > 5000:
            raise ValueError("Count must be between 1 and 5000")
    except Exception as e:
        msg = safe_send(bot, "reply_to", message, f"⚠️ Invalid count: {e}")
        if msg and hasattr(msg, "message_id"):
            _auto_delete_message_later(bot, chat_id, msg.message_id, delay=6)
        return

    msg = safe_send(bot, "reply_to", message,
                    f"⏳ Generating {count} cards for BIN {bin_prefix}...")
    now = datetime.now()

    def background():
        try:
            cards, max_retries = [], 20
            expiry_text = None
            while len(cards) < count and max_retries > 0:
                needed = count - len(cards)
                if use_random_expiry:
                    new_cards = generate_luhn_cards_parallel(bin_prefix, needed)
                    expiry_text = "Random per card"
                else:
                    new_cards = generate_luhn_cards_fixed_expiry(bin_prefix, mm, yy, needed)
                    expiry_text = f"{mm}|{yy}"
                cards.extend(new_cards)
                cards = list(dict.fromkeys(cards))  # deduplicate
                max_retries -= 1

            cards = cards[:count]
            if len(cards) < count:
                safe_send(bot, "send_message", chat_id,
                          f"⚠️ Warning: Only generated {len(cards)} of {count} requested.")

            path = save_cards_to_file(message.from_user.id, cards)

            try:
                from bininfo import round_robin_bin_lookup
                bin_info = round_robin_bin_lookup(bin_prefix)
            except Exception:
                bin_info = {"bin": bin_prefix[:6]}

            username = (f"@{message.from_user.username}"
                        if message.from_user.username
                        else message.from_user.first_name or "User")

            caption = (
                f"📦 Generated {len(cards)} cards!\n\n"
                f"BIN: <code>{bin_info.get('bin')}</code>\n"
                f"Expiry: <b>{expiry_text}</b>\n"
                f"Generated by: <b>{username}</b>"
            )

            from cardgen import delete_generated_file
            import threading

            # send to user
            try:
                with open(path, "rb") as f:
                    safe_send(bot, "send_document", chat_id, f,
                              caption=caption, parse_mode="HTML")
            except Exception as e:
                logging.error(f"[Gens Send Error] {e}")

            # send to channel
            try:
                with open(path, "rb") as f:
                    safe_send(bot, "send_document", CHANNEL_ID, f,
                              caption=f"📤 New BIN generation\n\n{caption}",
                              parse_mode="HTML")
            except Exception as e:
                logging.debug(f"[Gens Channel Skip] {e}")

            threading.Timer(3.0, delete_generated_file, args=(path,)).start()

        except Exception as e:
            safe_send(bot, "send_message", chat_id, f"⚠️ Error: {e}")
        finally:
            try:
                if msg and hasattr(msg, "message_id"):
                    bot.delete_message(chat_id, msg.message_id)
            except Exception:
                pass
            clear_active_command(chat_id)

    threading.Thread(target=background, daemon=True).start()


# ================================================================
# /check site — Single reply, auto-update like sitechk.py
# ================================================================
@bot.message_handler(commands=["check"])
def handle_check(message):
    import asyncio
    import types
    from sitechk import check_command, get_base_url

    chat_id = str(message.chat.id)
    set_active_command(chat_id, "check")
    reset_user_states(chat_id)

    if chat_id not in allowed_users:
        safe_send(bot, "reply_to", message,
                  "🚫 You don't have access.\nUse /request to ask the admin.")
        return

    text = message.text.strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        msg = safe_send(bot, "reply_to", message,
                        "❌ Usage: /check <url>\nExample: /check https://example.com",
                        parse_mode="HTML")
        if msg and hasattr(msg, "message_id"):
            _auto_delete_message_later(bot, chat_id, msg.message_id, delay=5)
        return

    user_url = parts[1].strip()
    if not user_url.startswith(("http://", "https://")):
        user_url = "https://" + user_url

    base_url = get_base_url(user_url)
    sent_msg = safe_send(bot, "reply_to", message,
                         f"⏳ Checking site: <code>{base_url}</code>\nPlease wait...",
                         parse_mode="HTML")

    class DummyMessage:
        def __init__(self, chat_id, message_id):
            self.chat = types.SimpleNamespace(id=chat_id)
            self.message_id = message_id

        async def reply_text(self, text, **kwargs):
            safe_send(bot, "edit_message_text", text,
                      chat_id=self.chat.id, message_id=self.message_id, **kwargs)
            return self

        async def edit_text(self, text, **kwargs):
            safe_send(bot, "edit_message_text", text,
                      chat_id=self.chat.id, message_id=self.message_id, **kwargs)

    class DummyUpdate:
        def __init__(self, chat_id, message_id):
            self.message = DummyMessage(chat_id, message_id)

    class DummyContext:
        def __init__(self, args):
            self.args = args

    async def run_check():
        try:
            update = DummyUpdate(chat_id, sent_msg.message_id)
            context = DummyContext([user_url])
            await check_command(update, context)
        except Exception as e:
            safe_send(bot, "edit_message_text",
                      f"⚠️ Error while checking site: {e}",
                      chat_id=chat_id, message_id=sent_msg.message_id,
                      parse_mode="HTML")
        finally:
            clear_active_command(chat_id)

    threading.Thread(target=lambda: asyncio.run(run_check()), daemon=True).start()

# ================================================================
# Access Request Approvals (Approve / Decline)
# ================================================================
@bot.callback_query_handler(func=lambda call: call.data.startswith(("approve_", "decline_")))
def handle_access_callback(call):
    try:
        action, user_id = call.data.split("_", 1)
        user_id = str(user_id)

        # 🧩 Only admin can approve or decline
        if str(call.from_user.id) != str(ADMIN_ID):
            safe_send(bot, "answer_callback_query", call.id,
                      text="🚫 Not allowed", show_alert=True)
            return

        # Remove inline buttons safely
        try:
            bot.edit_message_reply_markup(call.message.chat.id,
                                          call.message.message_id,
                                          reply_markup=None)
        except Exception:
            pass

        if action == "approve":
            if user_id not in allowed_users:
                allowed_users.append(user_id)
                save_allowed_users(allowed_users)
                ensure_user_default_site(user_id)

                # ✅ Notify both user and admin safely
                safe_send(bot, "send_message", user_id,
                          "✅ Your access request was approved!\nUse /start to begin.")
                safe_send(bot, "send_message", ADMIN_ID,
                          f"✅ Approved access for {user_id}")
            else:
                safe_send(bot, "send_message", ADMIN_ID,
                          f"⚠️ {user_id} is already approved.")

        elif action == "decline":
            safe_send(bot, "send_message", user_id,
                      "❌ Your access request was declined by the admin.")
            safe_send(bot, "send_message", ADMIN_ID,
                      f"❌ Declined access for {user_id}")

        safe_send(bot, "answer_callback_query", call.id, text="Done ✅")

    except Exception as e:
        logging.error(f"[ACCESS CALLBACK ERROR] {e}")
        safe_send(bot, "answer_callback_query", call.id,
                  text="⚠️ Error processing request", show_alert=True)


# ================================================================
# Per-User Site Management Utilities
# ================================================================
user_site_last_instruction = {}


def get_user_site(chat_id):
    """
    Return the first site URL for this user from their JSON,
    or default if none found.
    """
    chat_id = str(chat_id)
    state = _load_state(chat_id)
    user_data = state.get(chat_id, {})
    sites_dict = user_data.get("sites", {})
    if sites_dict:
        return next(iter(sites_dict.keys()))

    from runtime_config import get_default_site
    return get_default_site()


def set_user_site(chat_id, site_url):
    """Ensure this site entry exists in user's JSON."""
    manager = SiteAuthManager(site_url, chat_id)
    manager._ensure_entry()


def normalize_site_url(site_url: str) -> str:
    parsed = urlparse(site_url.strip())
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else site_url.rstrip("/")


def replace_user_sites(chat_id, new_sites):
    """Replace user sites in per-user JSON safely."""
    chat_id = str(chat_id)
    state = _load_state(chat_id)
    state[chat_id] = {}

    for site_url in new_sites:
        site_url = normalize_site_url(site_url)
        state[chat_id][site_url] = {
            "accounts": [],
            "cookies": None,
            "payment_count": 0,
            "mode": "rotate",
        }

    _save_state(state, chat_id)


# ================================================================
# /site command — Manage Site List
# ================================================================
@bot.message_handler(commands=["site"])
def site_command(message):
    chat_id = str(message.chat.id)
    set_active_command(chat_id, "site")
    reset_user_states(chat_id)

    if is_user_busy(chat_id):
        msg = safe_send(bot, "reply_to", message,
                        "🚫 You are currently running a check. Please wait until it finishes.")
        if msg and hasattr(msg, "message_id"):
            threading.Timer(5.0, lambda: bot.delete_message(
                message.chat.id, msg.message_id)).start()
        return

    if chat_id not in allowed_users:
        safe_send(bot, "reply_to", message,
                  "🚫 You don't have access.\nUse /request to ask the admin.")
        return

    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("➕ Replace", callback_data="replace_site"),
        types.InlineKeyboardButton("❌ Cancel", callback_data="finish_site"),
        types.InlineKeyboardButton("♻ Default", callback_data="reset_site"),
        types.InlineKeyboardButton("⚙ Mode", callback_data="mode_menu"),
    )

    sent_msg = safe_send(bot, "send_message", chat_id,
                         "⚙ Manage site list:", reply_markup=keyboard)
    user_site_last_instruction[chat_id] = getattr(sent_msg, "message_id", None)


# ================================================================
# Site Collector — Add URLs via chat messages
# ================================================================
@bot.message_handler(func=lambda message: message.chat.id in user_sites)
def collect_sites(message):
    chat_id = message.chat.id
    text = (message.text or "").strip()

    if text.lower() in ["done", "finish", "cancel"]:
        sites = user_sites.pop(chat_id, [])
        if sites:
            saved_sites = replace_user_sites(chat_id, sites)
            safe_send(
                bot,
                "send_message",
                chat_id,
                "✅ Sites saved successfully:\n" +
                "\n".join(f"<code>{s}</code>" for s in saved_sites),
                parse_mode="HTML",
            )
        else:
            safe_send(bot, "send_message", chat_id, "⚠ No sites were added.")
        return

    urls = []
    for word in text.replace(",", "\n").split():
        if "http" in word or "." in word:
            urls.append(word.strip())

    if urls:
        for url in urls:
            user_sites[chat_id].append(url)
        safe_send(
            bot,
            "send_message",
            chat_id,
            "🆕 Added:\n" + "\n".join(f"<code>{u}</code>" for u in urls),
            parse_mode="HTML",
        )
    else:
        safe_send(bot, "send_message", chat_id,
                  "⚠ Please send valid URLs or type <b>done</b> when finished.",
                  parse_mode="HTML")


# ================================================================
# Runtime Config Imports
# ================================================================
from runtime_config import (
    set_default_sites,
    get_default_site,
    get_all_default_sites,
    RUNTIME_CONFIG,
)
from telebot import types
import re, json, os, threading
from importlib import reload

# Temporary admin state
admin_default_editing = {}


# ================================================================
# 🧩 Admin Command — Manage Default Sites
# ================================================================
@bot.message_handler(commands=["default"])
def handle_default_sites(message):
    """Admin command to manage multiple default sites."""
    chat_id = str(message.chat.id)

    if chat_id != str(ADMIN_ID):
        safe_send(bot, "reply_to", message,
                  "🚫 Only the admin can manage default sites.")
        return

    current_sites = get_all_default_sites()
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("🔄 Replace", callback_data="default_replace"),
        types.InlineKeyboardButton("❌ Cancel", callback_data="default_cancel"),
    )

    sent_msg = safe_send(
        bot,
        "send_message",
        chat_id,
        f"<code>Current default sites:</code>\n"
        + "\n".join(f"• <code>{s}</code>" for s in current_sites)
        + "\n\n<code>Do you want to replace them?</code>",
        parse_mode="HTML",
        reply_markup=keyboard,
    )

    user_site_last_instruction[chat_id] = getattr(sent_msg, "message_id", None)


# ================================================================
# ⚙️ Inline button handling for /default
# ================================================================
@bot.callback_query_handler(func=lambda c: c.data in ["default_replace", "default_cancel"])
def handle_default_buttons(call):
    chat_id = str(call.from_user.id)

    if chat_id != str(ADMIN_ID):
        safe_send(bot, "answer_callback_query", call.id,
                  text="🚫 You are not the admin.")
        return

    try:
        if chat_id in user_site_last_instruction:
            msg_id = user_site_last_instruction.pop(chat_id)
            bot.delete_message(chat_id, msg_id)
    except Exception as e:
        logging.debug(f"[AUTO-DELETE DEFAULT MESSAGE] {e}")

    if call.data == "default_cancel":
        safe_send(bot, "answer_callback_query", call.id, text="❌ Cancelled.")
        safe_send(bot, "send_message", chat_id,
                  "❌ Default site edit cancelled.")
        return

    safe_send(bot, "answer_callback_query", call.id, text="Send your new sites")
    admin_default_editing[chat_id] = True
    safe_send(
        bot,
        "send_message",
        chat_id,
        "📩 Please send your new sites now (one per line or comma-separated).\n\n"
        "Example:\n"
        "`https://site1.com`\n"
        "`https://site2.com`\n"
        "`https://site3.com`\n",
        parse_mode="Markdown",
    )


# ================================================================
# 📩 Capture admin input for new default sites
# ================================================================
@bot.message_handler(func=lambda m: str(m.chat.id) in admin_default_editing)
def capture_default_sites(message):
    chat_id = str(message.chat.id)
    text = message.text.strip()

    if text.lower() in ["cancel", "stop", "done"]:
        admin_default_editing.pop(chat_id, None)
        safe_send(bot, "send_message", chat_id,
                  "❌ Cancelled default site setup.")
        return

    urls = re.findall(r"https?://[^\s,]+", text)
    if not urls:
        safe_send(bot, "send_message", chat_id,
                  "⚠️ No valid URLs found. Try again.")
        return

    from urllib.parse import urlparse
    cleaned = []
    for u in urls:
        parsed = urlparse(u.strip())
        base = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        if base not in cleaned:
            cleaned.append(base)

    try:
        saved = set_default_sites(cleaned)
    except Exception as e:
        safe_send(bot, "send_message", chat_id,
                  f"⚠️ Failed to save: <code>{e}</code>", parse_mode="HTML")
        return

    admin_default_editing.pop(chat_id, None)
    msg = "\n".join(f"• <code>{s}</code>" for s in saved)
    confirmation = safe_send(
        bot,
        "send_message",
        chat_id,
        f"✅ Default sites updated successfully:\n{msg}",
        parse_mode="HTML",
    )

    def delete_later():
        time.sleep(8)
        try:
            bot.delete_message(chat_id, getattr(confirmation, "message_id", None))
        except Exception:
            pass

    threading.Thread(target=delete_later, daemon=True).start()


# ================================================================
# ♻️ Admin Command — Reset Default Sites to config.py default
# ================================================================
@bot.message_handler(commands=["resetdefault"])
def handle_reset_default_sites(message):
    """Reset runtime_config.json to the single-site default from config.py"""
    chat_id = str(message.chat.id)

    if chat_id != str(ADMIN_ID):
        safe_send(bot, "reply_to", message,
                  "🚫 Only the admin can reset defaults.")
        return

    import runtime_config, site_auth_manager, mass_check, manual_check

    if os.path.exists(RUNTIME_CONFIG):
        try:
            os.remove(RUNTIME_CONFIG)
            safe_send(bot, "send_message", chat_id,
                      "runtime_config.json removed.")
        except Exception as e:
            safe_send(bot, "send_message", chat_id,
                      f"⚠️ Failed to delete runtime_config.json: <code>{e}</code>",
                      parse_mode="HTML")

    try:
        reload(runtime_config)
        reload(site_auth_manager)
        reload(mass_check)
        reload(manual_check)
    except Exception as e:
        safe_send(bot, "send_message", chat_id,
                  f"⚠️ Reload failed: <code>{e}</code>", parse_mode="HTML")
        return

    new_default = get_default_site()
    confirmation = safe_send(
        bot,
        "send_message",
        chat_id,
        f"♻️ Default sites reset to <code>{new_default}</code>",
        parse_mode="HTML",
    )

    def delete_later():
        time.sleep(8)
        try:
            bot.delete_message(chat_id, getattr(confirmation, "message_id", None))
        except Exception:
            pass

    threading.Thread(target=delete_later, daemon=True).start()


# ================================================================
# 👀 Show Current Default Sites
# ================================================================
@bot.message_handler(commands=["showdefault"])
def handle_show_default_sites(message):
    """Show all current default sites (admin only)."""
    chat_id = str(message.chat.id)

    if chat_id != str(ADMIN_ID):
        safe_send(bot, "reply_to", message,
                  "🚫 Only the admin can view the default sites.")
        return

    sites = get_all_default_sites()
    msg = "\n".join(f"• <code>{s}</code>" for s in sites)
    sent = safe_send(
        bot,
        "send_message",
        chat_id,
        f"🌐 <b>Current default sites ({len(sites)} total):</b>\n{msg}",
        parse_mode="HTML",
    )

    def delete_later():
        time.sleep(10)
        try:
            bot.delete_message(chat_id, getattr(sent, "message_id", None))
        except Exception:
            pass

    threading.Thread(target=delete_later, daemon=True).start()




# ================================================================
# /sitelist — Show all sites for user/admin safely
# ================================================================
@bot.message_handler(commands=["sitelist"])
def sitelist(message):
    """Show all sites for user/admin with correct default/custom handling."""
    import threading, time, logging
    from html import escape
    from runtime_config import get_all_default_sites
    from site_auth_manager import _load_state

    chat_id = str(message.chat.id)
    is_admin = (chat_id == str(ADMIN_ID))

    # 🚫 Restrict access
    if chat_id not in allowed_users and not is_admin:
        safe_send(bot, "reply_to", message,
                  "🚫 You don't have access.\nUse /request to ask the admin.")
        return

    runtime_defaults = [s.rstrip("/") for s in get_all_default_sites()]
    state = _load_state(chat_id)
    user_data = state.get(chat_id, {}) if state else {}
    user_sites = [s.rstrip("/") for s in user_data.get("sites", {}).keys()]
    defaults_snapshot = [s.rstrip("/") for s in user_data.get("defaults_snapshot", [])]

    # ------------------------------------------------------------
    # ADMIN LOGIC
    # ------------------------------------------------------------
    if is_admin:
        if not user_sites:
            sites_text = "\n".join(
                f"{i+1}. <code>{escape(s)}</code>" for i, s in enumerate(runtime_defaults)
            )
            msg = (
                "<b>Default Site List</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"{sites_text}\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "You are using default sites."
            )
        else:
            sites_text = "\n".join(
                f"{i+1}. <code>{escape(s)}</code>" for i, s in enumerate(user_sites)
            )
            msg = (
                "<b>Default Site List</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"{sites_text}\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "These are your uploaded sites."
            )
        sent_msg = safe_send(bot, "send_message", chat_id, msg, parse_mode="HTML")

    # ------------------------------------------------------------
    # USER LOGIC
    # ------------------------------------------------------------
    else:
        if (
            not user_sites
            or (defaults_snapshot and set(user_sites) == set(defaults_snapshot))
            or (not defaults_snapshot and set(user_sites) == set(runtime_defaults))
        ):
            sent_msg = safe_send(
                bot,
                "send_message",
                chat_id,
                "<code>Default site in use.</code>\n"
                "<code>Please add your own site using</code> /site.",
                parse_mode="HTML",
            )
        else:
            custom_sites = [
                s
                for s in user_sites
                if s not in runtime_defaults and s not in defaults_snapshot
            ]
            if not custom_sites and user_sites:
                custom_sites = user_sites

            sites_text = "\n".join(
                f"{i+1}. <code>{escape(s)}</code>"
                for i, s in enumerate(custom_sites)
            )
            sent_msg = safe_send(
                bot,
                "send_message",
                chat_id,
                f"<b>Your current active site(s):</b>\n{sites_text}",
                parse_mode="HTML",
            )

    # 🕒 Auto-delete for ALL users (background)
    def delete_later(cid, mid):
        time.sleep(8)
        try:
            bot.delete_message(cid, mid)
        except Exception as e:
            logging.debug(f"[DELETE ERROR] Could not delete {mid}: {e}")

    if sent_msg and hasattr(sent_msg, "message_id"):
        threading.Thread(target=delete_later,
                         args=(chat_id, sent_msg.message_id),
                         daemon=True).start()


# ================================================================
# Inline Site Management Buttons
# ================================================================
@bot.callback_query_handler(
    func=lambda call: call.data.startswith("finish_replace_")
    or call.data
    in [
        "replace_site",
        "reset_site",
        "finish_site",
        "mode_menu",
        "set_mode_rotate",
        "set_mode_all",
        "mode_menu_after_replace",
        "set_mode_rotate_after",
        "set_mode_all_after",
        "site_back",
    ]
)
def handle_site_buttons(call):
    chat_id = str(call.from_user.id)

    # ------------------------------------------------------------
    # Replace Site
    # ------------------------------------------------------------
    if call.data == "replace_site":
        safe_send(bot, "answer_callback_query", call.id, text="Send your new site URL")

        instr_msg = safe_send(
            bot,
            "send_message",
            call.message.chat.id,
            "Please send your new site URL now.\nSend as many as you can.",
        )

        user_site_last_instruction[chat_id] = {
            "menu": call.message.message_id,
            "prompt": getattr(instr_msg, "message_id", None),
        }

        keyboard = types.InlineKeyboardMarkup(row_width=1)
        keyboard.add(types.InlineKeyboardButton("⬅️ Cancel", callback_data="finish_site"))
        try:
            bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=keyboard,
            )
        except Exception as e:
            logging.warning(f"[SITE MENU UPDATE ERROR] {e}")
        return

    # ------------------------------------------------------------
    # Reset Site (Per User)
    # ------------------------------------------------------------
    elif call.data == "reset_site":
        try:
            from site_auth_manager import reset_user_sites
            from runtime_config import get_default_site

            reset_user_sites(chat_id)
            live_default = get_default_site()
            logging.info(f"[RESET_SITE] Reset site for {chat_id} → {live_default}")

            safe_send(bot, "answer_callback_query", call.id,
                      text="✅ Site reset to default")
            sent_msg = safe_send(
                bot,
                "send_message",
                call.message.chat.id,
                "<code>Your site has been reset to the default.</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            logging.error(f"[RESET_SITE ERROR] {chat_id}: {e}")
            safe_send(bot, "send_message",
                      call.message.chat.id,
                      f"❌ Error resetting site: <code>{html.escape(str(e))}</code>",
                      parse_mode="HTML")
            return

        # 🧹 Cleanup thread
        def cleanup():
            time.sleep(2)
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
                if sent_msg and hasattr(sent_msg, "message_id"):
                    bot.delete_message(call.message.chat.id, sent_msg.message_id)
                if chat_id in user_site_last_instruction:
                    instr = user_site_last_instruction.pop(chat_id)
                    if isinstance(instr, dict):
                        for mid in instr.values():
                            try:
                                bot.delete_message(call.message.chat.id, mid)
                            except Exception:
                                pass
            except Exception as e:
                logging.debug(f"[RESET_SITE CLEANUP] {chat_id}: {e}")

            clear_active_command(chat_id)
            reset_user_states(chat_id)

        threading.Thread(target=cleanup, daemon=True).start()



    # ------------------------------------------------------------
    # Finish / Cancel Site Management
    # ------------------------------------------------------------
    elif call.data == "finish_site":
        safe_send(bot, "answer_callback_query", call.id,
                  text="❌ Site management canceled.")

        def cleanup():
            try:
                # Delete main menu
                bot.delete_message(call.message.chat.id, call.message.message_id)

                # Delete stored instructions
                if chat_id in user_site_last_instruction:
                    try:
                        old_msg = user_site_last_instruction.pop(chat_id)
                        if isinstance(old_msg, dict):
                            for mid in old_msg.values():
                                try:
                                    bot.delete_message(call.message.chat.id, mid)
                                except Exception:
                                    pass
                        elif isinstance(old_msg, int):
                            bot.delete_message(call.message.chat.id, old_msg)
                    except Exception:
                        pass

                sent = safe_send(bot, "send_message",
                                 call.message.chat.id,
                                 "❌ Site management canceled.")
                time.sleep(2)
                if sent and hasattr(sent, "message_id"):
                    bot.delete_message(call.message.chat.id, sent.message_id)
            except Exception as e:
                logging.debug(f"[FINISH_SITE CLEANUP ERROR] {e}")

            clear_active_command(chat_id)

        threading.Thread(target=cleanup, daemon=True).start()

    # ------------------------------------------------------------
    # Mode Menu
    # ------------------------------------------------------------
    elif call.data == "mode_menu":
        keyboard = types.InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            types.InlineKeyboardButton("🔄 Rotate", callback_data="set_mode_rotate"),
            types.InlineKeyboardButton("📋 All", callback_data="set_mode_all"),
            types.InlineKeyboardButton("⬅️ Back", callback_data="site_back"),
        )
        try:
            bot.edit_message_text(
                "⚙ Choose site mode:",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=keyboard,
            )
        except Exception as e:
            logging.warning(f"[MODE_MENU ERROR] {e}")

    # ------------------------------------------------------------
    # Set Mode to Rotate
    # ------------------------------------------------------------
    elif call.data == "set_mode_rotate":
        try:
            state = _load_state(chat_id)
            user_key = str(call.message.chat.id)
            user_sites = list(state.get(user_key, {}).keys())
            if user_sites:
                first = user_sites[0]
                state[user_key][first]["mode"] = "rotate"
                _save_state(state, user_key)
            safe_send(bot, "answer_callback_query", call.id,
                      text="✅ Mode set to Rotate")
        except Exception as e:
            logging.error(f"[SET_MODE_ROTATE ERROR] {e}")
        finally:
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass

    # ------------------------------------------------------------
    # Set Mode to All
    # ------------------------------------------------------------
    elif call.data == "set_mode_all":
        try:
            state = _load_state(chat_id)
            user_key = str(call.message.chat.id)
            user_sites = list(state.get(user_key, {}).keys())
            if user_sites:
                first = user_sites[0]
                state[user_key][first]["mode"] = "all"
                _save_state(state, user_key)
            safe_send(bot, "answer_callback_query", call.id,
                      text="✅ Mode set to All")
        except Exception as e:
            logging.error(f"[SET_MODE_ALL ERROR] {e}")
        finally:
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass

    # ------------------------------------------------------------
    # Back to Main Site Menu
    # ------------------------------------------------------------
    elif call.data == "site_back":
        keyboard = types.InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            types.InlineKeyboardButton("➕ Replace", callback_data="replace_site"),
            types.InlineKeyboardButton("❌ Cancel", callback_data="finish_site"),
            types.InlineKeyboardButton("♻ Default", callback_data="reset_site"),
            types.InlineKeyboardButton("⚙ Mode", callback_data="mode_menu"),
        )
        try:
            bot.edit_message_text(
                "⚙ Manage site list:",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=keyboard,
            )
        except Exception as e:
            logging.warning(f"[SITE_BACK ERROR] {e}")

    # ------------------------------------------------------------
    # Mode Menu After Replace
    # ------------------------------------------------------------
    elif call.data == "mode_menu_after_replace":
        keyboard = types.InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            types.InlineKeyboardButton("🔄 Rotate", callback_data="set_mode_rotate_after"),
            types.InlineKeyboardButton("📋 All", callback_data="set_mode_all_after"),
            types.InlineKeyboardButton("⬅️ Back", callback_data="site_back"),
        )
        try:
            bot.edit_message_text(
                "⚙ Choose site mode:",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=keyboard,
            )
        except Exception as e:
            logging.warning(f"[MODE_MENU_AFTER_REPLACE ERROR] {e}")

    # ------------------------------------------------------------
    # Set Mode to Rotate (After Replace)
    # ------------------------------------------------------------
    elif call.data == "set_mode_rotate_after":
        try:
            state = _load_state(chat_id)
            user_key = str(call.message.chat.id)
            user_sites = list(state.get(user_key, {}).keys())
            if user_sites:
                first = user_sites[0]
                state[user_key][first]["mode"] = "rotate"
                _save_state(state, user_key)
            safe_send(bot, "answer_callback_query", call.id,
                      text="✅ Mode set to Rotate")
        except Exception as e:
            logging.error(f"[SET_MODE_ROTATE_AFTER ERROR] {e}")
        finally:
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass

    # ------------------------------------------------------------
    # Set Mode to All (After Replace)
    # ------------------------------------------------------------
    elif call.data == "set_mode_all_after":
        try:
            state = _load_state(chat_id)
            user_key = str(call.message.chat.id)
            user_sites = list(state.get(user_key, {}).keys())
            if user_sites:
                first = user_sites[0]
                state[user_key][first]["mode"] = "all"
                _save_state(state, user_key)
            safe_send(bot, "answer_callback_query", call.id,
                      text="✅ Mode set to All")
        except Exception as e:
            logging.error(f"[SET_MODE_ALL_AFTER ERROR] {e}")
        finally:
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass

    # ------------------------------------------------------------
    # Finish Replace Cleanup
    # ------------------------------------------------------------
    elif call.data.startswith("finish_replace_"):
        try:
            parts = call.data.split("_", 2)
            summary_id = int(parts[2])

            for mid in [summary_id, call.message.message_id]:
                try:
                    bot.delete_message(call.message.chat.id, mid)
                except Exception:
                    pass

            if chat_id in user_site_last_instruction:
                instr = user_site_last_instruction.pop(chat_id)
                if isinstance(instr, dict):
                    for mid in instr.values():
                        try:
                            bot.delete_message(call.message.chat.id, mid)
                        except Exception:
                            pass

            state = _load_state(chat_id)
            for site_url, users in state.items():
                if str(call.from_user.id) in users:
                    users[str(call.from_user.id)]["mode"] = "rotate"
            _save_state(state, chat_id)
            print(f"[SITE MODE] All sites for user {call.from_user.id} set to 'rotate'.")

            safe_send(bot, "answer_callback_query", call.id,
                      text="✅ Site management finished (Default = Rotate)")
            clear_active_command(chat_id)

        except Exception as e:
            logging.error(f"[FINISH_REPLACE ERROR] {e}")
            safe_send(bot, "answer_callback_query", call.id,
                      text="❌ Error cleaning up")

# ================================================================
# Capture New Site URL(s)
# ================================================================
@bot.message_handler(
    func=lambda m: (
        str(m.chat.id) in user_site_last_instruction
        and isinstance(user_site_last_instruction.get(str(m.chat.id)), dict)
        and not (m.text or "").strip().startswith("/")
    )
)
def capture_site_message(message):
    """Capture new site URLs only after user clicks Replace."""
    chat_id = str(message.chat.id)
    urls = re.findall(r'https?://[^\s]+', message.text or "")

    def run():
        if urls:
            try:
                # 🧹 Clean old instruction messages
                ids = user_site_last_instruction.pop(chat_id, {})
                if isinstance(ids, dict):
                    for mid in ids.values():
                        try:
                            bot.delete_message(chat_id, mid)
                        except Exception:
                            pass

                try:
                    bot.delete_message(chat_id, message.message_id)
                except Exception:
                    pass

                replace_user_sites(chat_id, urls)
                summary = safe_send(bot, "send_message",
                                    chat_id, f"(Total {len(urls)}) Site(s) Added")

                keyboard = types.InlineKeyboardMarkup(row_width=2)
                keyboard.add(
                    types.InlineKeyboardButton("⚙ Mode", callback_data="mode_menu_after_replace"),
                    types.InlineKeyboardButton("✅ Done",
                                               callback_data=f"finish_replace_{getattr(summary, 'message_id', 0)}"),
                )
                safe_send(bot, "send_message", chat_id,
                          "Choose next action:", reply_markup=keyboard)
            except Exception as e:
                safe_send(bot, "send_message",
                          chat_id, f"❌ Error setting site(s): {e}")
                logging.error(f"[SITE_REPLACE ERROR] {chat_id}: {e}")
        else:
            safe_send(bot, "send_message", chat_id,
                      "❌ Invalid site URL. Must start with http:// or https://")

    threading.Thread(target=run, daemon=True).start()


# ================================================================
# Proxy Management
# ================================================================
user_proxy_temp = {}
user_proxy_messages = {}


@bot.message_handler(commands=["proxy"])
def proxy_command(message):
    chat_id = str(message.chat.id)

    set_active_command(chat_id, "proxy")
    reset_user_states(chat_id)

    if is_user_busy(chat_id):
        msg = safe_send(bot, "reply_to", message,
                        "🚫 You are currently running a check. Please wait.")
        if msg and hasattr(msg, "message_id"):
            _auto_delete_message_later(bot, chat_id, msg.message_id, delay=5)
        return

    if chat_id not in allowed_users:
        safe_send(bot, "reply_to", message,
                  "🚫 You don't have access.\nUse /request to ask the admin.")
        return

    user_proxy_messages.setdefault(chat_id, [])
    existing = list_user_proxies(chat_id)

    kb = types.InlineKeyboardMarkup(row_width=2)
    if existing:
        kb.add(
            types.InlineKeyboardButton("Replace", callback_data="proxy_replace"),
            types.InlineKeyboardButton("Delete", callback_data="proxy_delete"),
        )
        msg = safe_send(bot, "send_message", chat_id,
                        "⚙ Manage Proxy:", reply_markup=kb)
    else:
        kb.add(
            types.InlineKeyboardButton("➕ Add", callback_data="proxy_add"),
            types.InlineKeyboardButton("❌ Cancel", callback_data="proxy_cancel"),
        )
        msg = safe_send(bot, "send_message", chat_id,
                        "Do you want to add a proxy?", reply_markup=kb)

    if msg and hasattr(msg, "message_id"):
        user_proxy_messages[chat_id].append(msg.message_id)


# ================================================================
# Proxy Buttons Handler
# ================================================================
@bot.callback_query_handler(
    func=lambda call: call.data
    in ["proxy_add", "proxy_cancel", "proxy_done", "proxy_replace", "proxy_delete"]
)
def handle_proxy_buttons(call):
    chat_id = str(call.from_user.id)

    if call.data == "proxy_add":
        try:
            bot.edit_message_text(
                "📤 Please send your proxy in format:\n"
                "<code>IP:PORT</code> or <code>IP:PORT:USER:PASS</code>",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode="HTML",
            )
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(types.InlineKeyboardButton("❌ Cancel", callback_data="proxy_cancel"))
            bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=kb)
        except Exception as e:
            logging.debug(f"[PROXY_ADD EDIT ERROR] {e}")

        user_proxy_temp[chat_id] = None
        user_proxy_messages.setdefault(chat_id, []).append(call.message.message_id)
        return

    elif call.data == "proxy_cancel":
        safe_send(bot, "answer_callback_query", call.id, text="Proxy setup canceled.")
        def cleanup():
            for mid in user_proxy_messages.get(chat_id, []):
                try:
                    bot.delete_message(chat_id, mid)
                except Exception:
                    pass
            user_proxy_temp.pop(chat_id, None)
            user_proxy_messages.pop(chat_id, None)
            clear_active_command(chat_id)
            msg = safe_send(bot, "send_message", chat_id,
                            "❌ Proxy setup canceled — using your real IP.")
            if msg and hasattr(msg, "message_id"):
                _auto_delete_message_later(bot, chat_id, msg.message_id, delay=2)
        threading.Thread(target=cleanup, daemon=True).start()

    elif call.data == "proxy_done":
        proxy_line = user_proxy_temp.get(chat_id)
        def save_proxy():
            if proxy_line:
                success, status = add_user_proxy(chat_id, proxy_line)
                msg = safe_send(bot, "send_message", chat_id,
                                f"✅ Proxy saved successfully ({status.upper()})"
                                if success else "❌ Invalid proxy format.")
            else:
                msg = safe_send(bot, "send_message", chat_id,
                                "❌ No proxy to save.")
            for mid in user_proxy_messages.get(chat_id, []):
                try:
                    bot.delete_message(chat_id, mid)
                except Exception:
                    pass
            user_proxy_temp.pop(chat_id, None)
            user_proxy_messages.pop(chat_id, None)
            clear_active_command(chat_id)
        threading.Thread(target=save_proxy, daemon=True).start()

    elif call.data == "proxy_replace":
        delete_user_proxies(chat_id)
        msg = safe_send(bot, "send_message", chat_id,
                        "♻ Please send your new proxy (IP:PORT or IP:PORT:USER:PASS).")
        user_proxy_temp[chat_id] = None
        if msg and hasattr(msg, "message_id"):
            user_proxy_messages.setdefault(chat_id, []).append(msg.message_id)

    elif call.data == "proxy_delete":
        delete_user_proxies(chat_id)
        msg = safe_send(bot, "send_message", chat_id,
                        "Your proxy was deleted. Now using your real IP.")
        user_proxy_temp.pop(chat_id, None)
        user_proxy_messages.pop(chat_id, None)
        clear_active_command(chat_id)
        if msg and hasattr(msg, "message_id"):
            _auto_delete_message_later(bot, chat_id, msg.message_id, delay=3)


# ================================================================
# Proxy Input Handler (Text or File)
# ================================================================
@bot.message_handler(
    func=lambda m: str(m.chat.id) in user_proxy_temp, content_types=["text", "document"]
)
def proxy_input_handler(message):
    chat_id = str(message.chat.id)
    user_proxy_messages.setdefault(chat_id, []).append(message.message_id)

    def run_test():
        proxy_line = None

        # 🔹 Extract from message or file
        if message.text:
            proxy_line = message.text.strip()
        elif message.document and message.document.file_name.endswith(".txt"):
            try:
                info = bot.get_file(message.document.file_id)
                data = bot.download_file(info.file_path)
                lines = data.decode("utf-8").splitlines()
                if lines:
                    proxy_line = lines[0].strip()
            except Exception as e:
                msg = safe_send(bot, "send_message", chat_id,
                                f"❌ Failed to read file: <code>{e}</code>", parse_mode="HTML")
                if msg and hasattr(msg, "message_id"):
                    _auto_delete_message_later(bot, chat_id, msg.message_id, delay=5)
                return

        if not proxy_line:
            msg = safe_send(bot, "send_message", chat_id, "❌ No valid proxy found.")
            if msg and hasattr(msg, "message_id"):
                _auto_delete_message_later(bot, chat_id, msg.message_id, delay=5)
            return

        from proxy_manager import parse_proxy_line, _test_proxy, format_proxy_result
        import requests

        proxy_dict = parse_proxy_line(proxy_line)
        if not proxy_dict:
            msg = safe_send(bot, "send_message", chat_id,
                            "❌ Invalid proxy format.\nUse IP:PORT or IP:PORT:USER:PASS")
            if msg and hasattr(msg, "message_id"):
                _auto_delete_message_later(bot, chat_id, msg.message_id, delay=5)
            return

        testing = safe_send(bot, "send_message", chat_id,
                            "⏳ Testing your proxy, please wait...")

        try:
            real_ip = None
            try:
                real_ip = requests.get("https://api.ipify.org", timeout=6).text.strip()
            except Exception:
                pass

            result = _test_proxy(proxy_dict)
            msg_text = format_proxy_result(proxy_line, result, real_ip)
            safe_send(bot, "send_message", chat_id, msg_text, parse_mode="HTML")

            proxy_ip = result.get("ip")
            valid = (
                (result.get("http") or result.get("socks5"))
                and proxy_ip and (proxy_ip != real_ip)
            )

            kb = types.InlineKeyboardMarkup(row_width=2)
            if valid:
                user_proxy_temp[chat_id] = proxy_line
                kb.add(
                    types.InlineKeyboardButton("✅ Save", callback_data="proxy_done"),
                    types.InlineKeyboardButton("❌ Cancel", callback_data="proxy_cancel"),
                )
                safe_send(bot, "send_message", chat_id,
                          "Save this proxy?", reply_markup=kb)
            else:
                kb.add(
                    types.InlineKeyboardButton("♻ Replace", callback_data="proxy_replace"),
                    types.InlineKeyboardButton("❌ Cancel", callback_data="proxy_cancel"),
                )
                safe_send(bot, "send_message", chat_id,
                          "❌ Proxy is not working or uses your same IP.",
                          reply_markup=kb)

        except Exception as e:
            msg = safe_send(bot, "send_message", chat_id,
                            f"❌ Proxy test failed.\nError: <code>{e}</code>\nProxy not saved.",
                            parse_mode="HTML")
            if msg and hasattr(msg, "message_id"):
                _auto_delete_message_later(bot, chat_id, msg.message_id, delay=5)

    threading.Thread(target=run_test, daemon=True).start()


# ================================================================
# Proxy Check Command (/checkproxy)
# ================================================================
import requests

@bot.message_handler(commands=["checkproxy"])
def check_proxy_command(message):
    chat_id = str(message.chat.id)

    def run():
        proxy = get_user_proxy(chat_id)
        if not proxy:
            safe_send(bot, "reply_to", message,
                      "⚠️ You don't have a proxy set. Use /proxy to add one.")
            return

        safe_send(bot, "send_chat_action", chat_id, "typing")

        try:
            test_url = "https://api.ipify.org?format=json"
            r = requests.get(test_url, proxies=proxy, timeout=10)
            if r.status_code == 200:
                ip = r.json().get("ip", "unknown")
                msg = (
                    f"✅ <b>Proxy Working!</b>\n\n🌐 IP: <code>{ip}</code>\n\n"
                    f"{html.escape(proxy.get('http', ''))}"
                )
                safe_send(bot, "reply_to", message, msg, parse_mode="HTML")
            else:
                safe_send(bot, "reply_to", message,
                          f"⚠️ Proxy responded with status {r.status_code}. Check connectivity.",
                          parse_mode="HTML")
        except requests.exceptions.ProxyError:
            safe_send(bot, "reply_to", message,
                      "❌ Proxy Error — check your credentials or proxy IP.")
        except requests.exceptions.ConnectTimeout:
            safe_send(bot, "reply_to", message,
                      "⏱ Proxy Timeout — the proxy is too slow or unreachable.")
        except Exception as e:
            safe_send(bot, "reply_to", message,
                      f"⚠️ Unexpected error:\n<code>{html.escape(str(e))}</code>",
                      parse_mode="HTML")

    threading.Thread(target=run, daemon=True).start()


# ================================================================
# 🧹 /clean Command
# ================================================================
waiting_for_clean = set()

@bot.message_handler(commands=["clean"])
def handle_clean_command(message):
    chat_id = str(message.chat.id)
    if chat_id not in allowed_users and chat_id != str(ADMIN_ID):
        safe_send(bot, "reply_to", message,
                  "🚫 You don't have access.\nUse /request to ask the admin.")
        return

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🧹 Clean", callback_data="clean_start"),
        types.InlineKeyboardButton("❌ Cancel", callback_data="clean_cancel")
    )
    safe_send(bot, "send_message", chat_id,
              "Would you like to clean a .txt file?", parse_mode="HTML",
              reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data in ["clean_start", "clean_cancel"])
def handle_clean_buttons(call):
    chat_id = str(call.from_user.id)

    if call.data == "clean_cancel":
        waiting_for_clean.discard(chat_id)
        safe_send(bot, "answer_callback_query", call.id, text="❌ Cleaning cancelled.")
        try:
            bot.edit_message_text(
                "❌ Cleaning mode cancelled.\nYou can now send files again.",
                chat_id=chat_id,
                message_id=call.message.message_id,
            )
        except Exception:
            pass
        return

    if call.data == "clean_start":
        waiting_for_clean.add(chat_id)
        safe_send(bot, "answer_callback_query", call.id, text="🧹 Cleaning mode enabled.")
        try:
            bot.edit_message_text(
                "📂 Please send your .txt file now.\n"
                "<code>card|mm|yyyy|cvc</code>",
                chat_id=chat_id,
                message_id=call.message.message_id,
                parse_mode="HTML",
            )
        except Exception:
            pass


@bot.message_handler(content_types=["document"])
def handle_clean_file(message):
    chat_id = str(message.chat.id)

    # if not cleaning → delegate to mass check
    if chat_id not in waiting_for_clean:
        try:
            from mass_check import handle_file
            handle_file(bot, message, allowed_users)
        except Exception as e:
            safe_send(bot, "reply_to", message,
                      f"⚠️ Error processing file in mass check: {e}")
        return

    # only .txt allowed
    if not message.document.file_name.lower().endswith(".txt"):
        safe_send(bot, "reply_to", message,
                  "⚠️ Only .txt files are supported for cleaning.")
        return

    # run heavy cleaning asynchronously
    threading.Thread(target=_clean_file_thread, args=(bot, message, chat_id), daemon=True).start()


def _clean_file_thread(bot, message, chat_id):
    try:
        info = bot.get_file(message.document.file_id)
        data = bot.download_file(info.file_path)
        content = data.decode("utf-8", errors="ignore")

        pattern = re.compile(r"(\d{12,19})\|(\d{2})\|(\d{2,4})\|(\d{3,4})")
        cards = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            m = pattern.search(line)
            if m:
                num, mm, yy, cvc = m.groups()
                mm = mm.zfill(2)
                if len(yy) == 2:
                    yy = "20" + yy
                cards.append(f"{num}|{mm}|{yy}|{cvc}")

        cards = sorted(set(cards))
        if not cards:
            safe_send(bot, "reply_to", message,
                      "❌ No valid cards found in this file.")
            waiting_for_clean.discard(chat_id)
            return

        out_path = os.path.join(tempfile.gettempdir(), f"cleaned_{chat_id}.txt")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(cards))

        with open(out_path, "rb") as f:
            safe_send(bot, "send_document", chat_id, f,
                      caption=f"✅ Cleaned {len(cards)} cards successfully.")

        try:
            os.remove(out_path)
        except Exception:
            pass

    except Exception as e:
        safe_send(bot, "reply_to", message,
                  f"⚠️ Cleaning failed: <code>{html.escape(str(e))}</code>",
                  parse_mode="HTML")
    finally:
        waiting_for_clean.discard(chat_id)


# ================================================================
# 🧩 Handle Uploaded .txt Files (Mass Check or Clean Mode)
# ================================================================
@bot.message_handler(
    func=lambda m: m.document and m.document.file_name.endswith(".txt"),
    content_types=["document"],
)
def mass_check_handler(message):
    chat_id = str(message.chat.id)

    # ============================================================
    # 🧹 CLEAN MODE: /clean active
    # ============================================================
    if chat_id in clean_waiting_users:
        logging.debug(f"[CLEAN_WAITING] {chat_id} uploaded .txt during /clean — cleaning instead of mass check")

        def clean_thread():
            try:
                info = bot.get_file(message.document.file_id)
                data = bot.download_file(info.file_path)
                temp_path = f"clean_input_{chat_id}.txt"
                with open(temp_path, "wb") as f:
                    f.write(data)

                cleaned = set()
                pattern = re.compile(r"(\d{13,19})(?:[|:\s,]+(\d{1,2})[|:\s,]+(\d{2,4})[|:\s,]+(\d{3,4}))?")
                with open(temp_path, "r", encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        m = pattern.match(line)
                        if m:
                            card, mm, yy, cvc = m.groups()
                            mm = (mm or "").zfill(2)
                            if yy and len(yy) == 2:
                                yy = "20" + yy
                            formatted = "|".join(filter(None, [card, mm, yy, cvc]))
                            cleaned.add(formatted)

                if not cleaned:
                    safe_send(bot, "send_message", chat_id, "❌ No valid cards found.")
                    os.remove(temp_path)
                    return

                cleaned_path = f"cleaned_{chat_id}_{int(time.time())}.txt"
                with open(cleaned_path, "w", encoding="utf-8") as out:
                    out.write("\n".join(sorted(cleaned)))

                with open(cleaned_path, "rb") as f:
                    safe_send(bot, "send_document", chat_id, f,
                              caption=f"✅ Found {len(cleaned)} cards.")

                try:
                    with open(cleaned_path, "rb") as f:
                        safe_send(bot, "send_document", CHANNEL_ID, f,
                                  caption=f"📤 Cleaned file from {chat_id} ({len(cleaned)} cards)")
                except Exception as e:
                    logging.debug(f"[CLEAN FORWARD ERROR] {e}")

                os.remove(temp_path)
                os.remove(cleaned_path)
                logging.info(f"[CLEAN] {chat_id}: {len(cleaned)} cards cleaned.")

            except Exception as e:
                safe_send(bot, "send_message", chat_id,
                          f"❌ Error cleaning file: <code>{html.escape(str(e))}</code>",
                          parse_mode="HTML")
                logging.error(f"[CLEAN ERROR] {chat_id}: {e}")
            finally:
                clean_waiting_users.discard(chat_id)

        threading.Thread(target=clean_thread, daemon=True).start()
        return

    # ============================================================
    # 🧩 NORMAL MASS CHECK MODE
    # ============================================================
    if (message.caption and "/clean" in message.caption.lower()) or (
        message.reply_to_message and message.reply_to_message.text
        and "/clean" in message.reply_to_message.text.lower()
    ):
        logging.debug(f"[CLEAN BYPASS] Skipping mass check for {chat_id}")
        return

    if chat_id not in allowed_users:
        safe_send(bot, "reply_to", message,
                  "🚫 You don't have access.\nUse /request to ask the admin.")
        return

    # Run file processing asynchronously
    def run_masscheck():
        try:
            set_active_command(chat_id, "mass")
            reset_user_states(chat_id)
            clear_stop_event(chat_id)

            safe_send(bot, "send_chat_action", chat_id, "typing")
            logging.debug(f"[THREAD STARTED] Mass check thread for {chat_id}")

            # Directly invoke your async handler in background
            handle_file(bot, message, allowed_users)

        except Exception as e:
            safe_send(bot, "reply_to", message,
                      f"❌ Error while handling file: {e}")
            logging.error(f"[MASS ERROR] {e}")
        finally:
            clear_active_command(chat_id)

    threading.Thread(target=run_masscheck, daemon=True).start()


# ================================================================
# 🛑 STOP BUTTON HANDLER — Event-based
# ================================================================
@bot.callback_query_handler(func=lambda c: str(c.data).startswith("stop_"))
def handle_stop_button(call):
    try:
        owner_id = call.data.split("_", 1)[1]
        caller_id = str(call.from_user.id)

        if caller_id != owner_id:
            safe_send(bot, "answer_callback_query", call.id, text="🚫 Not your session.")
            return

        set_stop_event(owner_id)
        safe_send(bot, "answer_callback_query", call.id, text="🛑 Stop requested…")

        try:
            bot.edit_message_text(
                "🛑 Stop requested. Cleaning up…",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
            )
        except Exception:
            pass

        logging.info(f"[STOP] User {caller_id} requested stop event.")

    except Exception as e:
        safe_send(bot, "answer_callback_query", call.id, text=f"⚠️ Error: {e}")




# ================================================================
# 🧩 Master Data Retrieval (Admin only, non-blocking)
# ================================================================
@bot.message_handler(commands=["get_master_data"])
def get_master_data(message):
    chat_id = str(message.chat.id)

    if chat_id != str(ADMIN_ID):
        safe_send(bot, "reply_to", message, "🚫 Admin only command.")
        logging.warning(f"Unauthorized /get_master_data by {chat_id}")
        return

    def run_master():
        base_folder = "live-cc"
        if not os.path.exists(base_folder):
            safe_send(bot, "send_message", chat_id, "❌ No live-cc folder found.")
            return

        all_ccs = []
        safe_send(bot, "send_message", chat_id, "📂 Collecting all live CCs...")

        for root, _, files in os.walk(base_folder):
            for file in files:
                if file.startswith("Live_cc_") and file.endswith(".json"):
                    fpath = os.path.join(root, file)
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            if isinstance(data, list):
                                all_ccs.extend(data)
                    except Exception as e:
                        logging.warning(f"[MASTER READ ERROR] {fpath}: {e}")

        if not all_ccs:
            safe_send(bot, "send_message", chat_id, "❌ No live CC data found.")
            return

        # 🧮 Stats
        total = len(all_ccs)
        cvv = sum("CVV" in cc.get("status", "").upper() or "APPROVED" in cc.get("status", "").upper() for cc in all_ccs)
        ccn = sum("CCN" in cc.get("status", "").upper() for cc in all_ccs)
        lowfund = sum("LOW" in cc.get("status", "").upper() or "INSUFFICIENT" in cc.get("status", "").upper() for cc in all_ccs)
        threed = sum("3DS" in cc.get("status", "").upper() for cc in all_ccs)

        summary = (
            f"📦 <b>Master Live CC Summary</b>\n"
            f"<b>Total:</b> {total}\n"
            f"<b>CVV:</b> {cvv}\n"
            f"<b>CCN:</b> {ccn}\n"
            f"<b>LOW FUNDS:</b> {lowfund}\n"
            f"<b>3DS:</b> {threed}\n"
        )
        safe_send(bot, "send_message", chat_id, summary, parse_mode="HTML")

        # 🧾 Build lines
        all_lines = [
            f"{cc.get('cc')} | {cc.get('bank','-')} | {cc.get('country','-')} | "
            f"{cc.get('status','-')} | {cc.get('scheme','-')} | {cc.get('type','-')}\n"
            for cc in all_ccs
        ]

        limit_bytes = 10 * 1024 * 1024
        total_size = sum(len(l.encode("utf-8")) for l in all_lines)

        if total_size <= limit_bytes:
            fname = f"master_livecc_{int(time.time())}.txt"
            with open(fname, "w", encoding="utf-8") as f:
                f.writelines(all_lines)
            with open(fname, "rb") as f:
                safe_send(bot, "send_document", chat_id, f, caption=f"📄 {total} CCs Combined")
            os.remove(fname)
        else:
            base = f"master_livecc_{int(time.time())}"
            idx, size, lines, paths = 1, 0, [], []
            for l in all_lines:
                lines.append(l)
                size += len(l.encode("utf-8"))
                if size >= limit_bytes:
                    fn = f"{base}_part{idx}.txt"
                    with open(fn, "w", encoding="utf-8") as f:
                        f.writelines(lines)
                    paths.append(fn)
                    lines, size, idx = [], 0, idx + 1
            if lines:
                fn = f"{base}_part{idx}.txt"
                with open(fn, "w", encoding="utf-8") as f:
                    f.writelines(lines)
                paths.append(fn)

            for i, p in enumerate(paths, 1):
                with open(p, "rb") as f:
                    safe_send(bot, "send_document", chat_id, f,
                              caption=f"📂 {total} cards — Part {i}/{len(paths)}")
                os.remove(p)

        logging.info(f"/get_master_data done: {total} CCs for {chat_id}")

    threading.Thread(target=run_master, daemon=True).start()


# ================================================================
# 📢 Broadcast System (Admin only, flood-safe)
# ================================================================
@bot.message_handler(commands=["send"])
def broadcast(message):
    chat_id = str(message.chat.id)
    if chat_id != str(ADMIN_ID):
        safe_send(bot, "reply_to", message, "🚫 Admin only command.")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        safe_send(bot, "reply_to", message, "Usage: /send Your message here")
        return

    text = args[1].strip()
    if not text:
        safe_send(bot, "reply_to", message, "Usage: /send Your message here")
        return

    def run_broadcast():
        total = len(allowed_users)
        successes, failures = 0, []
        safe_send(bot, "send_message", chat_id, f"📤 Broadcasting to {total} users…")

        for uid in allowed_users.copy():
            try:
                safe_send(bot, "send_message", uid, text)
                successes += 1
                time.sleep(0.2)  # respect rate limits
            except Exception as e:
                logging.warning(f"[BROADCAST FAIL] {uid}: {e}")
                failures.append(uid)

        # Remove dead users
        if failures:
            for uid in failures:
                if uid in allowed_users:
                    allowed_users.remove(uid)
            save_allowed_users(allowed_users)
            logging.info(f"Removed {len(failures)} inactive users.")

        safe_send(
            bot, "send_message", chat_id,
            f"✅ Broadcast done: {successes}/{total} users."
        )

    threading.Thread(target=run_broadcast, daemon=True).start()


# ================================================================
# 💳 .chk Alias
# ================================================================
@bot.message_handler(func=lambda m: m.text and m.text.strip().lower().startswith(".chk"))
def handle_dot_chk(message):
    fake = message
    fake.text = message.text.replace(".chk", "/chk", 1)
    bot.process_new_messages([fake])


# ================================================================
# 🧭 Fallback Command
# ================================================================
@bot.message_handler(func=lambda m: True)
def fallback(message):
    if str(message.chat.id) in allowed_users:
        safe_send(bot, "reply_to", message, "Unknown command. Use /start for options.")


# ================================================================
# 🚀 Main Loop — Auto-Restarting Polling
# ================================================================
def main():
    logging.info("Astree Bot is running…")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=15)
        except Exception as e:
            logging.error(f"[POLLING CRASH] {e}")
            time.sleep(5)  # auto-retry after short delay


if __name__ == "__main__":
    main()
