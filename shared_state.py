# ================================================================
# 🧩 Shared State Module
# ================================================================

user_busy = {}

# ================================================================
# 🔧 Proxy Format Parser (shared by proxy_manager & proxy_check)
# ================================================================
import re

def parse_proxy_line(line: str):
    """Parses proxies in common formats and returns a dict or None if invalid."""
    if not line:
        return None

    line = line.strip().replace(" ", "")

    # Try multiple proxy patterns
    patterns = [
        # host:port:user:pass
        r"^([\w\.-]+):(\d{2,6}):([^:@]+):(.+)$",
        # user:pass@host:port
        r"^([^:@]+):([^:@]+)@([\w\.-]+):(\d{2,6})$",
        # user:pass:host:port
        r"^([^:@]+):([^:@]+):([\w\.-]+):(\d{2,6})$",
        # host:port@user:pass
        r"^([\w\.-]+):(\d{2,6})@([^:@]+):([^:@]+)$",
        # host:port (no auth)
        r"^([\w\.-]+):(\d{2,6})$",
    ]

    for p in patterns:
        m = re.match(p, line)
        if m:
            g = m.groups()
            if len(g) == 2:
                host, port = g
                return {"host": host, "port": int(port)}
            elif len(g) == 4:
                # Try to figure out which pattern matched
                if "@" in line or line.count(":") > 2:
                    # If it's host:port:user:pass
                    if g[0].replace(".", "").isalpha() or g[0].count(".") >= 1:
                        return {"host": g[0], "port": int(g[1]), "user": g[2], "pass": g[3]}
                    # Else maybe user:pass@host:port
                    elif g[2].replace(".", "").isalpha() or g[2].count(".") >= 1:
                        return {"host": g[2], "port": int(g[3]), "user": g[0], "pass": g[1]}
    return None

# ============================================================
# 🧾 Shared Function — Save Live CC JSON (per user & worker)
# ============================================================

import os
import json
import threading
import logging
from datetime import datetime

_livecc_folder_lock = threading.Lock()

def save_live_cc_to_json(user_id: str, worker_id: int, live_data: dict):
    """
    Thread-safe shared function.
    Each worker writes to its own live file:
        live-cc/<user_id>/Live_cc_<user_id>_<worker_id>.json
    """
    folder = os.path.join("live-cc", str(user_id))

    # Ensure per-user folder exists safely
    with _livecc_folder_lock:
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as e:
            logging.warning(f"[LIVE JSON] Failed to create folder {folder}: {e}")
            return

    file_path = os.path.join(folder, f"Live_cc_{user_id}_{worker_id}.json")

    # Add timestamp
    live_data["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        # Each worker writes to its own file (no shared writes)
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = []
        else:
            existing = []

        existing.append(live_data)

        # Write atomically with .tmp → replace
        tmp_path = f"{file_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, file_path)

        logging.info(f"[LIVE JSON] Worker {worker_id} → {file_path}")
    except Exception as e:
        logging.error(f"[LIVE JSON ERROR] User {user_id}, Worker {worker_id}: {e}")
