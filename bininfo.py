import requests
import re
import threading
import logging
import json
import os
logging.getLogger("bininfo").disabled = True
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bininfo")

BIN_CACHE_FILE = "bin_cache.json"

BIN_LOOKUP_SERVICES = [
    {
        "name": "binlist_net",
        "url_template": "https://lookup.binlist.net/{}",
        "headers": {},
        "post": False,
        "parse": lambda data: {
            "scheme": data.get("scheme", "N/A").upper(),
            "type": data.get("type", "N/A").upper(),
            "brand": data.get("brand", "STANDARD").upper(),
            "bank": data.get("bank", {}).get("name", "Unknown Bank"),
            "country": data.get("country", {}).get("name", "Unknown Country"),
        },
    },
    {
        "name": "antipublic_bins",
        "url": "https://bins.antipublic.cc/bins/",
        "headers": {},
        "post": False,
        "parse": lambda data: {
            "scheme": data.get("scheme", "N/A").upper(),
            "type": data.get("type", "Unknown").upper(),
            "brand": data.get("brand", "Unknown").upper(),
            "bank": data.get("bank", "Unknown Bank"),
            "country": data.get("country_name", "Unknown Country"),
        },
    },
]

_cache = {}
_cache_lock = threading.Lock()


def _load_cache_from_file():
    global _cache
    if os.path.exists(BIN_CACHE_FILE):
        try:
            with open(BIN_CACHE_FILE, "r", encoding="utf-8") as f:
                _cache = json.load(f)
            logger.info(f"Loaded {len(_cache)} BINs from cache file")
        except Exception as e:
            logger.warning(f"Could not load BIN cache: {e}")


def _save_cache_to_file():
    try:
        with open(BIN_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_cache, f, indent=2, ensure_ascii=False)
        logger.debug(f"Saved {_cache.__len__()} BINs to cache file")
    except Exception as e:
        logger.error(f"Failed to save BIN cache: {e}")


def _normalize_bin_info(parsed: dict) -> dict:
    """Clean up redundant brand/type/scheme info for display."""
    brand = parsed.get("brand", "").upper().strip()
    ctype = parsed.get("type", "").upper().strip()
    scheme = parsed.get("scheme", "").upper().strip()

    # Remove generic "CARD" suffix
    if brand.endswith("CARD"):
        brand = brand.replace("CARD", "").strip()

    # Collect unique parts
    parts = []
    for val in [brand, ctype, scheme]:
        if val and val not in parts and val not in ["UNKNOWN", "N/A"]:
            parts.append(val)

    parsed["brand"] = brand
    parsed["type"] = ctype
    parsed["scheme"] = scheme
    parsed["display_clean"] = " - ".join(parts) if parts else "Unknown"

    return parsed


def _lookup_single_service(bin_number, service, proxy=None, timeout_seconds=10):
    try:
        headers = service.get("headers", {}).copy()
        params = {}
        auth = service.get("auth")

        if "url_template" in service:
            url = service["url_template"].format(bin_number)
        else:
            url = service["url"].rstrip("/") + "/" + bin_number

        if service.get("post", False):
            params = service.get("auth", {}).copy()
            params["bin"] = bin_number
            resp = requests.post(url, headers=headers, data=params, proxies=proxy, timeout=timeout_seconds)
        else:
            if auth:
                headers.update(auth)
            resp = requests.get(url, headers=headers, params=params, proxies=proxy, timeout=timeout_seconds)

        if resp.status_code == 200:
            data = resp.json()
            parsed = service["parse"](data)
            parsed["bin"] = bin_number

            # Clean country string
            parsed["country"] = re.sub(r"\s*\(.*?\)", "", parsed["country"]).strip()

            # 🔹 Normalize BIN info
            parsed = _normalize_bin_info(parsed)

            # ✅ Only save good results (skip if Unknown everywhere)
            if all("Unknown" not in str(v) and v not in ["N/A"] for v in parsed.values()):
                with _cache_lock:
                    _cache[bin_number] = parsed
                    _save_cache_to_file()
                    logger.info(f"Cached BIN {bin_number}: {parsed}")

            return parsed
        else:
            logger.warning(f"Error from {service['name']}: HTTP {resp.status_code}")
            return None

    except Exception as e:
        logger.error(f"Exception during request to {service['name']}: {e}")
        return None


def round_robin_bin_lookup(card_number: str, proxy=None, timeout_seconds=10):
    """
    Lookup BIN info for the given card number.
    Returns dict: {bin, scheme, type, brand, bank, country, display_clean}.
    """
    bin_number = card_number[:6]

    with _cache_lock:
        if bin_number in _cache:
            logger.debug(f"Cache hit for BIN {bin_number}: {_cache[bin_number]}")
            return _cache[bin_number]

    # Try first service
    result = _lookup_single_service(bin_number, BIN_LOOKUP_SERVICES[0], proxy, timeout_seconds)
    if result:
        logger.info(f"BIN {bin_number} resolved by {BIN_LOOKUP_SERVICES[0]['name']}")
        return result

    # Fallback service
    result = _lookup_single_service(bin_number, BIN_LOOKUP_SERVICES[1], proxy, timeout_seconds)
    if result:
        logger.info(f"BIN {bin_number} resolved by {BIN_LOOKUP_SERVICES[1]['name']}")
        return result

    # Default result (⚠ not cached)
    default = {
        "bin": bin_number,
        "scheme": "Unknown",
        "type": "Unknown",
        "brand": "Unknown",
        "bank": "Unknown Bank",
        "country": "Unknown Country",
        "display_clean": "Unknown",
    }
    logger.warning(f"All BIN services failed for {bin_number}. Returning default.")
    return default


# ✅ Load cache on module import
_load_cache_from_file()
