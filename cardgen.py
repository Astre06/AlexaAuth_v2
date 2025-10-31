import os
import random
import time
from datetime import datetime
import concurrent.futures
from dateutil.relativedelta import relativedelta

# Folder for generated files
OUTPUT_DIR = "gens"


# ===============================================================
# Directory helper
# ===============================================================
def ensure_output_dir():
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR, exist_ok=True)


# ===============================================================
# Card utility functions
# ===============================================================
def luhn_check(card_number):
    total = 0
    reverse_digits = card_number[::-1]
    for i, digit in enumerate(reverse_digits):
        n = int(digit)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def gen_placeholder_card(bin_prefix, mm, yy):
    bin_digits = "".join(ch for ch in bin_prefix if ch.isdigit())
    if len(bin_digits) >= 16:
        number_body = bin_digits[:16]
    else:
        needed = 16 - len(bin_digits)
        number_body = bin_digits + "".join(random.choice("0123456789") for _ in range(needed))
    number_body = number_body[:-1] + random.choice("0123456789")
    cvc = "".join(random.choice("0123456789") for _ in range(3))
    return f"{number_body}|{mm}|{yy}|{cvc}"


def is_valid_card_format(card_str):
    try:
        n, mm, yy, cvc = card_str.split("|")
        if not (13 <= len(n) <= 19):
            return False
        if not (mm.isdigit() and 1 <= int(mm) <= 12):
            return False
        if not (yy.isdigit() and len(yy) == 2):
            return False
        if not (len(cvc) == 3 and cvc.isdigit()):
            return False
        if not luhn_check(n):
            return False
        return True
    except:
        return False


def get_random_expiry(today=None, years_ahead=7):
    """Generate random future month/year."""
    if today is None:
        today = datetime.today()
    future_date = today + relativedelta(years=years_ahead)
    while True:
        year = random.randint(today.year, future_date.year)
        if year == today.year:
            month = random.randint(today.month, 12)
        elif year == future_date.year:
            month = random.randint(1, future_date.month)
        else:
            month = random.randint(1, 12)
        gen_date = datetime(year, month, 1)
        if gen_date >= datetime(today.year, today.month, 1):
            break
    yy = str(year)[2:]
    mm = str(month).zfill(2)
    return mm, yy


# ===============================================================
# Card generation functions
# ===============================================================
def generate_luhn_cards_parallel(bin_prefix, count, workers=5):
    """Generate random expiry cards."""
    ensure_output_dir()
    valid_cards = []
    attempts = 0
    max_attempts = count * 10
    today = datetime.today()

    def worker():
        mm, yy = get_random_expiry(today)
        card = gen_placeholder_card(bin_prefix, mm, yy)
        if is_valid_card_format(card):
            return card
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        while len(valid_cards) < count and attempts < max_attempts:
            futures = [executor.submit(worker) for _ in range(10)]
            for future in concurrent.futures.as_completed(futures):
                card = future.result()
                if card:
                    valid_cards.append(card)
                    if len(valid_cards) >= count:
                        break
            attempts += 10
    return valid_cards


def generate_luhn_cards_fixed_expiry(bin_prefix, mm, yy, count, workers=5):
    """Generate fixed expiry cards."""
    ensure_output_dir()
    valid_cards = []
    attempts = 0
    max_attempts = count * 10

    def worker():
        card = gen_placeholder_card(bin_prefix, mm, yy)
        if is_valid_card_format(card):
            return card
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        while len(valid_cards) < count and attempts < max_attempts:
            futures = [executor.submit(worker) for _ in range(10)]
            for future in concurrent.futures.as_completed(futures):
                card = future.result()
                if card:
                    valid_cards.append(card)
                    if len(valid_cards) >= count:
                        break
            attempts += 10
    return valid_cards


# ===============================================================
# Save to file
# ===============================================================
def save_cards_to_file(user_id, cards):
    """Save generated cards to gens/ directory."""
    ensure_output_dir()
    timestamp = int(time.time())
    filename = f"{user_id}_{timestamp}.txt"
    path = os.path.join(OUTPUT_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(cards))
    return path

def delete_generated_file(path):
    """Delete generated BIN file after sending."""
    try:
        if os.path.exists(path):
            os.remove(path)
            print(f"[CLEANUP] Deleted generated file: {path}")
    except Exception as e:
        print(f"[CLEANUP ERROR] {e}")

# ===============================================================
# Module test (manual only)
# ===============================================================
if __name__ == "__main__":
    print("Testing BIN generator (manual run only)...")
    cards = generate_luhn_cards_parallel("453968", 10)
    for c in cards:
        print(c)
    path = save_cards_to_file("testuser", cards)
    print(f"Saved to {path}")
