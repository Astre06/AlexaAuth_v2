# ============================================================
# 🔧 Simple Bot Config with Runtime Fallback
# ============================================================
import os
import json

# Default values
DEFAULTS = {
    "BOT_TOKEN": "7942038573:AAFYfHHR3e616zgbvJlNMjLCROMxPb4opC0",
    "CHANNEL_ID": "-1002963282470",
    "ADMIN_ID": "6679042143",
    "DEFAULT_API_URL": "https://www.seed2design.co.uk",
    "PAYMENT_LIMIT": 15,
    "RETRY_DELAY": 1,
    "RETRY_COUNT": 1,
    "MAX_WORKERS": 5,
    "BATCH_SIZE": 20,
    "DELAY_BETWEEN_BATCHES": 3,
}

CONFIG_FILE = "runtime_config.json"


def load_config():
    """
    Load configuration dynamically.
    - If config.json exists → merge overrides.
    - If missing or invalid → safely fall back to DEFAULTS.
    """
    cfg = DEFAULTS.copy()

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
                if isinstance(user_cfg, dict):
                    cfg.update(user_cfg)
        except Exception as e:
            print(f"[CONFIG ERROR] Failed to load config.json, using defaults: {e}")
    else:
        print("[CONFIG] No config.json found — using built-in defaults")

    return cfg


# Optional: export variables directly for static import compatibility
_cfg = load_config()

BOT_TOKEN = _cfg["BOT_TOKEN"]
CHANNEL_ID = _cfg["CHANNEL_ID"]
ADMIN_ID = _cfg["ADMIN_ID"]
DEFAULT_API_URL = _cfg["DEFAULT_API_URL"]

PAYMENT_LIMIT = _cfg["PAYMENT_LIMIT"]
RETRY_DELAY = _cfg["RETRY_DELAY"]
RETRY_COUNT = _cfg["RETRY_COUNT"]

MAX_WORKERS = _cfg["MAX_WORKERS"]
BATCH_SIZE = _cfg["BATCH_SIZE"]
DELAY_BETWEEN_BATCHES = _cfg["DELAY_BETWEEN_BATCHES"]


