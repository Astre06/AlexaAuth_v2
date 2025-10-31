# ============================================================
# ⚙️ Runtime Config (Dynamic default sites + Safe auto-creation)
# ============================================================

import json
import os
import random
from config import DEFAULT_API_URL  # permanent fallback

RUNTIME_CONFIG = "runtime_config.json"


# ------------------------------------------------------------
# 🔧 URL Sanitizer
# ------------------------------------------------------------
def _sanitize_url(url: str) -> str:
    """Ensure the given URL is valid and has http/https, returning its base domain."""
    if not url or not isinstance(url, str):
        return DEFAULT_API_URL

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    # Keep only scheme + netloc
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if not parsed.netloc:
        return DEFAULT_API_URL
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


# ------------------------------------------------------------
# 🧩 Ensure runtime_config.json exists
# ------------------------------------------------------------
def _ensure_runtime_config_exists():
    """Ensure runtime_config.json exists and contains a valid DEFAULT_API_URL."""
    need_repair = False
    data = {}

    if os.path.exists(RUNTIME_CONFIG):
        try:
            with open(RUNTIME_CONFIG, "r", encoding="utf-8") as f:
                data = json.load(f)
            url = str(data.get("DEFAULT_API_URL", "")).strip()
            if not url or not url.startswith("http"):
                print(f"[RUNTIME_CONFIG] Invalid or missing URL found ({url}) — fixing...")
                need_repair = True
        except Exception as e:
            print(f"[RUNTIME_CONFIG] File unreadable or corrupt: {e}")
            need_repair = True
    else:
        print("[RUNTIME_CONFIG] File missing — will create new one...")
        need_repair = True

    if need_repair:
        try:
            url = _sanitize_url(DEFAULT_API_URL)
            data = {"DEFAULT_API_URL": url, "EXTRA_SITES": []}
            with open(RUNTIME_CONFIG, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            print(f"[RUNTIME_CONFIG] Created/Repaired → {url}")
        except Exception as e:
            print(f"[RUNTIME_CONFIG] Failed to create/repair runtime_config.json: {e}")


# ------------------------------------------------------------
# 🌐 Get a single default site (random if multiple)
# ------------------------------------------------------------
def get_default_site() -> str:
    """Get one default site. If multiple exist, pick one randomly."""
    _ensure_runtime_config_exists()

    try:
        with open(RUNTIME_CONFIG, "r", encoding="utf-8") as f:
            data = json.load(f)

        main = _sanitize_url(data.get("DEFAULT_API_URL", DEFAULT_API_URL))
        extras = [s for s in data.get("EXTRA_SITES", []) if isinstance(s, str)]
        all_sites = [main] + extras
        return random.choice(all_sites) if all_sites else _sanitize_url(DEFAULT_API_URL)

    except Exception as e:
        print(f"[RUNTIME_CONFIG] Read error, using fallback: {e}")
        return _sanitize_url(DEFAULT_API_URL)


# ------------------------------------------------------------
# 🌐 Get all default sites (main + extras)
# ------------------------------------------------------------
def get_all_default_sites() -> list[str]:
    """Return a list of all default sites (main + extras)."""
    _ensure_runtime_config_exists()
    try:
        with open(RUNTIME_CONFIG, "r", encoding="utf-8") as f:
            data = json.load(f)

        main = _sanitize_url(data.get("DEFAULT_API_URL", DEFAULT_API_URL))
        extras = [s for s in data.get("EXTRA_SITES", []) if isinstance(s, str)]
        sites = [main] + extras
        return list(dict.fromkeys(sites))  # deduplicate
    except Exception as e:
        print(f"[RUNTIME_CONFIG] Error reading sites: {e}")
        return [DEFAULT_API_URL]


# ------------------------------------------------------------
# 💾 Save (set) new default site(s)
# ------------------------------------------------------------
def set_default_sites(sites: list[str]) -> list[str]:
    """
    Save one or more default sites (replaces all).
    Example structure saved:
    {
      "DEFAULT_API_URL": "https://main.com",
      "EXTRA_SITES": ["https://site2.com", "https://site3.com"]
    }
    """
    if not sites:
        print("[RUNTIME_CONFIG] No sites provided to save.")
        return []

    sanitized = [_sanitize_url(s) for s in sites if s]
    main = sanitized[0]
    extras = sanitized[1:]

    try:
        data = {"DEFAULT_API_URL": main, "EXTRA_SITES": extras}
        with open(RUNTIME_CONFIG, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"[RUNTIME_CONFIG] ✅ Saved defaults: {sanitized}")
        return sanitized
    except Exception as e:
        print(f"[RUNTIME_CONFIG] Write error: {e}")
        return [DEFAULT_API_URL]


# ------------------------------------------------------------
# ➕ Append a single site without overwriting
# ------------------------------------------------------------
def append_default_site(url: str) -> list[str]:
    """Add one new site to EXTRA_SITES without replacing existing ones."""
    _ensure_runtime_config_exists()
    try:
        with open(RUNTIME_CONFIG, "r", encoding="utf-8") as f:
            data = json.load(f)

        base = _sanitize_url(url)
        main = data.get("DEFAULT_API_URL", DEFAULT_API_URL)
        extras = [s for s in data.get("EXTRA_SITES", []) if isinstance(s, str)]

        if base not in extras and base != main:
            extras.append(base)

        data["DEFAULT_API_URL"] = _sanitize_url(main)
        data["EXTRA_SITES"] = extras

        with open(RUNTIME_CONFIG, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        print(f"[RUNTIME_CONFIG] Added new site: {base}")
        return [main] + extras
    except Exception as e:
        print(f"[RUNTIME_CONFIG] Append failed: {e}")
        return get_all_default_sites()


# ------------------------------------------------------------
# 🧩 Backward compatibility for single-site setter
# ------------------------------------------------------------
def set_default_site(url: str) -> str:
    """Backward-compatible wrapper for one-site updates."""
    sites = set_default_sites([url])
    return sites[0] if sites else DEFAULT_API_URL


# ✅ Ensure file always exists on import
_ensure_runtime_config_exists()
