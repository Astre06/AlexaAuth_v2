# ============================================================
# 🌐 ADVANCED PER-USER PROXY MANAGER
# ============================================================
# Features:
#  • Each user has own JSON: proxies/proxies_<chat_id>.json
#  • Supports multiple proxies with round-robin rotation
#  • Thread-safe atomic file save
#  • Validates proxy formats (IP:PORT, IP:PORT:USER:PASS, etc.)
#  • Tests via https://api.ipify.org
#  • Saves all proxies (even if dead), with last_status
#  • Logs everything to proxy_debug.log
# ============================================================

import os
import json
import re
import threading
import time
import requests
from datetime import datetime
import logging
from shared_state import parse_proxy_line

# ------------------------------------------------------------
# Logging setup
# ------------------------------------------------------------
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    filename="logs/proxy_debug.log",
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
)
logger = logging.getLogger("proxy_manager")

# ------------------------------------------------------------
# File lock
# ------------------------------------------------------------
_proxy_lock = threading.Lock()


def _get_user_proxy_file(chat_id: str):
    """Return the path to the user's proxy JSON file."""
    os.makedirs("proxies", exist_ok=True)
    return os.path.join("proxies", f"proxies_{chat_id}.json")


# ============================================================
# 🔧 Proxy Format Parser
# ============================================================
import re

def parse_proxy_line(line: str):
    """
    Parse proxy string into a dict compatible with requests.
    Supports:
      - ip:port
      - host:port
      - host:port:user:pass
      - host:port@user:pass
      - user:pass@host:port
      - user:pass:host:port
    Handles complex usernames/passwords with -, _, ., and numbers.
    """
    if isinstance(line, dict):
        return line
    if not line:
        return None

    line = line.strip().replace(" ", "").replace(",", "")

    # 🧩 host:port
    m = re.match(r"^([\w\.-]+):(\d{2,6})$", line)
    if m:
        host, port = m.groups()
        return {"host": host, "port": port}

    # 🧩 host:port:user:pass
    m = re.match(r"^([\w\.-]+):(\d{2,6}):([^:@]+):(.+)$", line)
    if m:
        host, port, user, pwd = m.groups()
        return {"host": host, "port": port, "user": user, "pass": pwd}

    # 🧩 host:port@user:pass
    m = re.match(r"^([\w\.-]+):(\d{2,6})@([^:@]+):(.+)$", line)
    if m:
        host, port, user, pwd = m.groups()
        return {"host": host, "port": port, "user": user, "pass": pwd}

    # 🧩 user:pass@host:port
    m = re.match(r"^([^:@]+):(.+)@([\w\.-]+):(\d{2,6})$", line)
    if m:
        user, pwd, host, port = m.groups()
        return {"host": host, "port": port, "user": user, "pass": pwd}

    # 🧩 user:pass:host:port
    m = re.match(r"^([^:@]+):(.+):([\w\.-]+):(\d{2,6})$", line)
    if m:
        user, pwd, host, port = m.groups()
        return {"host": host, "port": port, "user": user, "pass": pwd}

    return None



# ============================================================
# 🧪 Proxy Tester
# ============================================================
def _test_proxy(proxy_dict, timeout=10, retries=2, retry_delay=1.0):
    """
    Proxy tester with retry support.
    Detects HTTP/SOCKS5 support, rotation, external IP, and latency.
    Validates that the proxy IP differs from the user's real IP.
    Retries failed tests up to `retries` times.
    """
    import time
    import requests

    TEST_URL = "https://api.ipify.org"
    result = {
        "http": False,
        "socks5": False,
        "rotating": False,
        "ip": "Unknown",
        "speed_ms": 0.0,
    }

    # 🔹 Step 0: Get user's real IP (for comparison)
    try:
        real_ip = requests.get(TEST_URL, timeout=6).text.strip()
    except Exception:
        real_ip = None

    def test_connection(proxy_scheme):
        """Try connecting using a given scheme (http or socks5) with retries."""
        if proxy_dict.get("user") and proxy_dict.get("pass"):
            auth = f"{proxy_dict['user']}:{proxy_dict['pass']}@"
        else:
            auth = ""

        proxy_str = f"{proxy_scheme}://{auth}{proxy_dict['host']}:{proxy_dict['port']}"
        proxies = {"http": proxy_str, "https": proxy_str}

        for attempt in range(1, retries + 1):
            try:
                start = time.perf_counter()
                r = requests.get(TEST_URL, proxies=proxies, timeout=timeout, verify=False)
                elapsed = (time.perf_counter() - start) * 1000
                if r.status_code == 200:
                    return r.text.strip(), elapsed
            except Exception as e:
                logger.warning(f"[TEST] Attempt {attempt}/{retries} failed for {proxy_str}: {e}")
                time.sleep(retry_delay)
        raise Exception(f"All {retries} attempts failed for {proxy_scheme}")

    # --- Step 1: Try HTTP proxy
    try:
        ip, speed = test_connection("http")
        if real_ip and ip == real_ip:
            raise Exception("Proxy IP matches direct IP — not a real proxy.")
        result["http"] = True
        result["ip"] = ip
        result["speed_ms"] = speed
    except Exception as e:
        logger.info(f"[HTTP TEST FAILED] {e}")

    # --- Step 2: Try SOCKS5 proxy if HTTP failed
    if not result["http"]:
        try:
            ip, speed = test_connection("socks5")
            if real_ip and ip == real_ip:
                raise Exception("Proxy IP matches direct IP — not a real proxy.")
            result["socks5"] = True
            result["ip"] = ip
            result["speed_ms"] = speed
        except Exception as e:
            logger.info(f"[SOCKS5 TEST FAILED] {e}")

    # --- Step 3: Rotation test (only if proxy worked)
    if result["http"] or result["socks5"]:
        try:
            ip1, _ = test_connection("http" if result["http"] else "socks5")
            ip2, _ = test_connection("http" if result["http"] else "socks5")
            if ip1 != ip2:
                result["rotating"] = True
        except Exception:
            pass

    # ✅ Final safety: block saving if proxy IP == direct IP
    if not result["ip"] or (real_ip and result["ip"] == real_ip):
        result["http"] = result["socks5"] = False
        result["ip"] = real_ip or "Unknown"
        result["speed_ms"] = 0.0
        result["rotating"] = False

    return result




def format_proxy_result(proxy_input: str, result: dict, real_ip: str = None) -> str:
    """
    Return a Telegram message for proxy test result.
    Includes strict validation: marks as failed if proxy IP == real IP.
    """
    http_ok = "✅" if result.get("http") else "❌"
    socks_ok = "✅" if result.get("socks5") else "❌"
    rotating = "✅" if result.get("rotating") else "❌"
    ip = result.get("ip", "Unknown")
    speed = result.get("speed_ms", 0.0)

    # 🧩 Check for same IP as direct connection
    if real_ip and ip == real_ip:
        return (
            f"❌ Proxy failed — using same IP as direct connection.\n\n"
            f"<b>Proxy:</b> <code>{proxy_input}</code>\n"
            f"External IP: {ip}\n"
            f"Speed: {speed:.2f} ms\n"
            f"HTTP: {http_ok} | 🧦 SOCKS5: {socks_ok}\n"
            f"Testing Failed, Please Try again."
        )

    # ✅ Proxy passed
    if result.get("http") or result.get("socks5"):
        return (
            f"✅Proxy is live and working!\n"
            f"External IP: {ip}\n"
            f"Speed: {speed:.2f} ms\n"
            f"Rotating: {rotating}\n"
            f"HTTP: {http_ok} | SOCKS5: {socks_ok}"
        )

    # ❌ Dead proxy
    return (
        f"❌ Proxy seems dead.\n"
        f"<b>Proxy:</b> <code>{proxy_input}</code>\n"
        f"HTTP: {http_ok} | 🧦 SOCKS5: {socks_ok}\n"
        f"Rotating: {rotating}\n"
        f"Speed: {speed:.2f} ms"
    )







# ============================================================
# 📂 File Operations (Thread-Safe)
# ============================================================
def _load_user_proxies(chat_id: str):
    """Load user proxy JSON safely."""
    path = _get_user_proxy_file(chat_id)
    if not os.path.exists(path):
        return {"proxies": [], "last_index": 0}

    with _proxy_lock:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[LOAD ERROR] {path}: {e}")
            return {"proxies": [], "last_index": 0}


def _save_user_proxies(chat_id: str, data: dict):
    """Atomically save user proxies."""
    path = _get_user_proxy_file(chat_id)
    tmp = path + ".tmp"
    with _proxy_lock:
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
            logger.info(f"[SAVE] Updated proxies for user {chat_id} ({len(data['proxies'])} proxies).")
        except Exception as e:
            logger.error(f"[SAVE ERROR] {chat_id}: {e}")


# ============================================================
# ✳️ Core Operations
# ============================================================
def add_user_proxy(chat_id: str, proxy_line: str, bot=None):
    """
    Add a proxy for this user.
    Now fully independent — checks and saves only if alive.
    """
    chat_id = str(chat_id)
    parsed = parse_proxy_line(proxy_line)
    if not parsed:
        if bot:
            bot.send_message(chat_id, "❌ Invalid proxy format.", parse_mode="HTML")
        logger.warning(f"[ADD] Invalid format for {chat_id}: {proxy_line}")
        return False, "Invalid proxy format"

    data = _load_user_proxies(chat_id)

    # 🔍 Full internal test
    result = _test_proxy(parsed)

    status = "live" if result.get("http") or result.get("socks5") else "dead"
    ip = result.get("ip", "Unknown")

    msg = format_proxy_result(proxy_line, result)
    if bot:
        bot.send_message(chat_id, msg, parse_mode="HTML")

    # 🚫 Only save if working proxy
    if status == "dead":
        if bot:
            bot.send_message(chat_id, "⚠️ Proxy failed. Not saved.", parse_mode="HTML")
        logger.info(f"[ADD] Dead proxy ignored for {chat_id}: {proxy_line}")
        return False, "dead"

    entry = {
        "raw": proxy_line,
        "parsed": parsed,
        "last_status": status,
        "last_checked": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ip": ip,
    }

    data["proxies"].append(entry)
    _save_user_proxies(chat_id, data)
    logger.info(f"[ADD] Added proxy for {chat_id} ({status.upper()}): {proxy_line}")
    return True, status



def replace_user_proxies(chat_id: str, proxy_lines: list[str], bot=None):
    """
    Replace all proxies for this user with verified live ones only.
    """
    chat_id = str(chat_id)
    new_entries = []

    for line in proxy_lines:
        parsed = parse_proxy_line(line)
        if not parsed:
            continue

        result = _test_proxy(parsed)
        status = "live" if result.get("http") or result.get("socks5") else "dead"
        ip = result.get("ip", "Unknown")

        msg = format_proxy_result(line, result)
        if bot:
            bot.send_message(chat_id, msg, parse_mode="HTML")

        # ✅ Only save working proxies
        if status == "live":
            new_entries.append({
                "raw": line,
                "parsed": parsed,
                "last_status": status,
                "last_checked": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ip": ip,
            })
        else:
            logger.info(f"[REPLACE] Dead proxy skipped: {line}")

    data = {"proxies": new_entries, "last_index": 0}
    _save_user_proxies(chat_id, data)
    logger.info(f"[REPLACE] {len(new_entries)} live proxies saved for {chat_id}")

    if bot:
        bot.send_message(chat_id, f"✅ Replaced with {len(new_entries)} working proxies.", parse_mode="HTML")

    return True




def delete_user_proxies(chat_id: str):
    path = _get_user_proxy_file(str(chat_id))
    if os.path.exists(path):
        try:
            os.remove(path)
            logger.info(f"[DELETE] Proxies deleted for {chat_id}")
            return True
        except Exception as e:
            logger.error(f"[DELETE ERROR] {chat_id}: {e}")
    return False


# ============================================================
# 🔁 Round-Robin Rotation
# ============================================================
def get_user_proxy(chat_id: str):
    """Return a ready-to-use proxy dict (host, port, user, pass)."""
    path = _get_user_proxy_file(chat_id)
    if not os.path.exists(path):
        return None

    try:
        with _proxy_lock:
            data = json.load(open(path, "r", encoding="utf-8"))
            proxies = data.get("proxies", [])
            if not proxies:
                return None

            # Pick the current proxy (based on last_index)
            index = data.get("last_index", 0) % len(proxies)
            entry = proxies[index]

            # Use the parsed section
            parsed = entry.get("parsed", entry)
            if not parsed or "host" not in parsed or "port" not in parsed:
                return None

            # Flatten for requests
            return {
                "host": parsed.get("host"),
                "port": parsed.get("port"),
                "user": parsed.get("user"),
                "pass": parsed.get("pass"),
            }

    except Exception as e:
        logger.error(f"Failed to load proxy for {chat_id}: {e}")
        return None



def list_user_proxies(chat_id: str):
    """Return list of all proxies with status info."""
    chat_id = str(chat_id)
    data = _load_user_proxies(chat_id)
    return data.get("proxies", [])


# ============================================================
# ✅ Example Usage (Standalone Test)
# ============================================================
if __name__ == "__main__":
    test_id = "12345"
    add_user_proxy(test_id, "127.0.0.1:8080")
    add_user_proxy(test_id, "user:pass@1.2.3.4:9090")
    print(get_user_proxy(test_id))
    print(list_user_proxies(test_id))
