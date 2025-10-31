import re
import requests
from telebot import types
from config import CHANNEL_ID
from shared_state import parse_proxy_line

# -------------------------------
# Helper: Get IP → country / region / ISP info
# -------------------------------


def get_ip_details(ip: str):
    """Fetch country/region/ISP info for a given IP, safely returning 'None' if unavailable."""
    if not ip or ip in ("Unknown", ""):
        return {
            "country": "None",
            "region": "None",
            "isp": "None",
            "ip": ip or "None",
        }

    try:
        resp = requests.get(
            f"http://ip-api.com/json/{ip}?fields=status,country,regionName,isp,query",
            timeout=8,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "success":
                return {
                    "country": data.get("country") or "None",
                    "region": data.get("regionName") or "None",
                    "isp": data.get("isp") or "None",
                    "ip": data.get("query") or (ip or "None"),
                }
    except Exception:
        pass

    # Default fallback if anything fails
    return {
        "country": "None",
        "region": "None",
        "isp": "None",
        "ip": ip or "None",
    }



# -------------------------------
# Parse supported proxy formats
# -------------------------------




# -------------------------------
# Actual proxy check
# -------------------------------
def check_proxy(proxy_dict):
    import time
    import requests

    host = proxy_dict["host"]
    port = proxy_dict["port"]
    user = proxy_dict.get("user")
    pwd = proxy_dict.get("pass")

    auth = f"{user}:{pwd}@" if user and pwd else ""
    http_proxy = f"http://{auth}{host}:{port}"
    socks_proxy = f"socks5://{auth}{host}:{port}"

    result = {
        "https": False,
        "socks5": False,
        "rotation": False,
        "static": None,
        "ip": "Unknown",
        "speed_ms": 0.0,
    }

    TEST_URL = "https://api.ipify.org"

    # 🔹 Get your real IP (to verify proxy hides it)
    try:
        real_ip = requests.get(TEST_URL, timeout=6).text.strip()
    except Exception:
        real_ip = None

    # ✅ Helper with retry
    def get_ip_with_retry(proxy, retries=2, delay=1.0):
        """Try fetching IP via proxy with limited retries."""
        for attempt in range(retries):
            try:
                start = time.perf_counter()
                r = requests.get(TEST_URL, proxies=proxy, timeout=8, verify=False)
                elapsed = (time.perf_counter() - start) * 1000
                if r.status_code == 200:
                    return r.text.strip(), elapsed
            except Exception:
                if attempt < retries - 1:
                    time.sleep(delay)
        return None, 0.0

    # ✅ Try HTTP proxy
    ip_http, speed_http = get_ip_with_retry({"http": http_proxy, "https": http_proxy})
    if ip_http and (not real_ip or ip_http != real_ip):
        result["https"] = True
        result["ip"] = ip_http
        result["speed_ms"] = speed_http

    # ✅ Try SOCKS5 proxy (only if HTTP failed)
    if not result["https"]:
        ip_socks, speed_socks = get_ip_with_retry({"http": socks_proxy, "https": socks_proxy})
        if ip_socks and (not real_ip or ip_socks != real_ip):
            result["socks5"] = True
            result["ip"] = ip_socks
            result["speed_ms"] = speed_socks

    # ✅ Check for rotation (2 requests, with retry)
    if result["https"] or result["socks5"]:
        try:
            proxy_choice = {"http": http_proxy, "https": http_proxy} if result["https"] else {"http": socks_proxy, "https": socks_proxy}
            ip1, _ = get_ip_with_retry(proxy_choice)
            ip2, _ = get_ip_with_retry(proxy_choice)
            if ip1 and ip2 and ip1 != ip2:
                result["rotation"] = True
        except Exception:
            pass

    result["static"] = not result["rotation"]

    # ✅ Final safety: mark as dead if proxy IP == real IP or no result
    if not result["ip"] or (real_ip and result["ip"] == real_ip):
        result["https"] = result["socks5"] = False
        result["ip"] = real_ip or "Unknown"
        result["speed_ms"] = 0.0
        result["rotation"] = False
        result["static"] = None

    return result





# -------------------------------
# Build message for Telegram
# -------------------------------
def build_proxy_report(proxy_input, check_result):
    ip_info = get_ip_details(check_result["ip"])

    https_status = "✅ Live" if check_result["https"] else "❌ Dead"
    socks_status = "✅ Live" if check_result["socks5"] else "❌ Dead"
    rotation_status = "✅" if check_result["rotation"] else "❌"
    static_status = "✅" if check_result["static"] else "❌"

    msg = (
        f"<b>Live proxy detected!</b>\n\n"
        f"<b>proxy:</b> <code>{proxy_input}</code>\n\n"
        f"HTTPS: {https_status}\n"
        f"SOCKS5: {socks_status}\n"
        f"Rotation: {rotation_status}\n"
        f"Static IP: {static_status}\n\n"
        f"IP: <code>{ip_info['ip']}</code>\n"
        f"Country: {ip_info['country']}\n"
        f"Region: {ip_info['region']}\n"
        f"ISP: {ip_info['isp']}\n\n"
        f"Your proxy is live and supports HTTP and/or SOCKS5.\n"
        f"BOT BY @Justnoob2"
    )
    return msg


# -------------------------------
# Register the /checkproxy command
# -------------------------------
def register_checkproxy(bot):
    @bot.message_handler(commands=["checkproxy"])
    def handle_checkproxy(message):
        chat_id = message.chat.id
        parts = message.text.strip().split(maxsplit=1)
        if len(parts) < 2:
            bot.send_message(
                chat_id,
                "👋 Hello! I'm the Proxy Checker Bot.\nUse /checkproxy &lt;proxy&gt;\nBOT BY @Justnoob2",
                parse_mode="HTML",
            )

            return

        proxy_input = parts[1].strip()
        parsed = parse_proxy_line(proxy_input)
        if not parsed:
            bot.send_message(
                chat_id,
                "❌ Invalid format. Supported formats:\n"
                "- host:port\n"
                "- host:port:user:pass\n"
                "- host:port@user:pass\n"
                "- user:pass@host:port\n"
                "- user:pass:host:port\n\nBOT BY @Justnoob2",
            )
            return

        # ⏳ send temporary message
        waiting_msg = bot.send_message(chat_id, "⏳ Checking your proxy... please wait")

        try:
            result = check_proxy(parsed)
            if result["https"] or result["socks5"]:
                msg = build_proxy_report(proxy_input, result)
            else:
                msg = f"❌ Dead proxy.\n\n<code>{proxy_input}</code>\n"
        except Exception as e:
            msg = f"⚠️ Error checking proxy: {e}"

        # ✅ send final result
        sent_msg = bot.send_message(chat_id, msg, parse_mode="HTML")

        # ✅ also send to your Channel ID
        # ✅ forward to channel + main + normal subscriber using notifier
        try:
            bot.send_message(CHANNEL_ID, msg, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            print(f"[DEBUG] Could not forward via notifier: {e}")


        # 🧹 delete “checking...” message after final message
        try:
            bot.delete_message(chat_id, waiting_msg.message_id)
        except Exception:
            pass
