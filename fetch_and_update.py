#!/usr/bin/env python3
"""Monitor MacBook Pro offers with >=36GB RAM on Alza.sk.

Stdlib only (no pip installs available in this sandbox).

Design constraints learned from live testing:
  * Alza fronts the category page with a Cloudflare human-verification
    challenge. A burst of requests trips it, so we make exactly ONE request
    per cycle and never retry inside a cycle.
  * The page is chunked and often truncates mid-stream (IncompleteRead).
    The partial body still contains the product markup, so we keep it.
  * robots.txt permits this category path but disallows /api/, so we only
    ever read the public HTML page - never their internal JSON endpoints.
  * A blocked cycle is recorded as status="blocked", never as "0 offers",
    so a failing monitor can't look like "prices are stable".

Usage:
    python3 fetch_and_update.py            # fetch + update state
    python3 fetch_and_update.py --probe    # single fetch, report only, no writes
    python3 fetch_and_update.py --from-file page.html   # parse a saved page
"""

import argparse
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html import unescape

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "https://www.alza.sk/lacne-macbook/18854758.htm"
UA = "Claude-User/1.0 (+https://www.anthropic.com/claude-user; personal price monitor)"

HISTORY_PATH = os.path.join(HERE, "price_history.json")
STATE_PATH = os.path.join(HERE, "state.json")

MIN_RAM_GB = 36

# Plausible Apple unified-memory sizes. Used to tell RAM from storage.
RAM_SIZES = {8, 16, 18, 24, 32, 36, 48, 64, 96, 128}


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------

def fetch(url=URL, timeout=45):
    """One request, no retries. Returns (status_label, body_bytes)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            try:
                return "ok", resp.read()
            except http.client.IncompleteRead as exc:
                # Chunked response truncated - the partial body is still usable.
                return "partial", exc.partial
    except http.client.IncompleteRead as exc:
        return "partial", exc.partial
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:
            body = b""
        return "http-%d" % exc.code, body
    except Exception as exc:
        return "error-%s" % type(exc).__name__, b""


def is_challenge(text):
    """True if the body is Alza's bot-verification interstitial."""
    low = text.lower()
    markers = (
        "confirm you are human",
        "potvrďte, že ste z mäsa",
        "potvrďte, že jste z masa",
        "bezpečnostné systémy",
        "bezpečnostní systémy",
    )
    if any(m.lower() in low for m in markers):
        return True
    # The challenge page is small and has no product grid.
    return "<title>alza.cz</title>" in low and "macbook" not in low


# --------------------------------------------------------------------------
# parse
# --------------------------------------------------------------------------

TAG_RE = re.compile(r"<[^>]+>")
# "3 299,-" / "3 299,00 €" / "3299 €" -> capture the numeric run
PRICE_RE = re.compile(r"(\d[\d\s ]{2,})(?:,-|,\d{2})?\s*(?:€|Eur|EUR)?")
GB_RE = re.compile(r"(\d+)\s*(GB|TB)", re.IGNORECASE)


def strip_tags(fragment):
    return re.sub(r"\s+", " ", unescape(TAG_RE.sub(" ", fragment))).strip()


def parse_price(fragment):
    """Pull the first plausible EUR price out of an HTML fragment."""
    text = strip_tags(fragment)
    for raw in re.findall(r"(\d[\d\s ]{2,}(?:,-|,\d{2})?)", text):
        digits = re.sub(r"[^\d]", "", raw.split(",")[0])
        if not digits:
            continue
        value = int(digits)
        # MacBook Pros live in the hundreds-to-tens-of-thousands range.
        if 300 <= value <= 100000:
            return float(value)
    return None


def extract_ram_gb(name):
    """Infer unified memory in GB from a product name.

    Apple listings read like 'M4 Max 36GB 1TB SSD': RAM first, storage after.
    Distinguishing rules:
      * TB is always storage.
      * With two GB figures, the smaller is RAM (36GB RAM vs 512GB SSD).
      * With one GB figure, accept it only if it's a real memory size.
    """
    tokens = [(int(v), u.upper()) for v, u in GB_RE.findall(name)]
    gb_values = [v for v, u in tokens if u == "GB"]
    has_tb = any(u == "TB" for _, u in tokens)

    if not gb_values:
        return None
    if len(gb_values) >= 2:
        candidate = min(gb_values)
        return candidate if candidate in RAM_SIZES else None
    candidate = gb_values[0]
    if has_tb:
        # Single GB figure alongside a TB disk -> that GB figure is memory.
        return candidate if candidate in RAM_SIZES else None
    return candidate if candidate in RAM_SIZES else None


def parse_offers(html):
    """Extract MacBook Pro offers with >=MIN_RAM_GB memory.

    Alza renders each product as a block containing an anchor with the
    product name and a nearby price. We locate candidate anchors and then
    scan a bounded window after each for the price, which keeps this
    resilient to class-name churn.
    """
    offers = []
    seen = set()

    for m in re.finditer(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
        href, inner = m.group(1), m.group(2)
        name = strip_tags(inner)
        if not name or "macbook" not in name.lower():
            continue
        if "macbook pro" not in name.lower():
            continue  # excludes Air / other models

        ram = extract_ram_gb(name)
        if ram is None or ram < MIN_RAM_GB:
            continue

        window = html[m.end():m.end() + 3000]
        price = parse_price(window)
        if price is None:
            continue

        url = href if href.startswith("http") else "https://www.alza.sk" + href
        key = (name, price)
        if key in seen:
            continue
        seen.add(key)
        offers.append({"name": name, "ram_gb": ram, "price_eur": price, "url": url})

    offers.sort(key=lambda o: o["price_eur"])
    return offers


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def run(write=True, from_file=None):
    now = datetime.now(timezone.utc).isoformat()

    if from_file:
        with open(from_file, "rb") as fh:
            body = fh.read()
        label = "file"
    else:
        label, body = fetch()

    text = body.decode("utf-8", errors="replace")
    blocked = (not text) or (label.startswith("http-")) or is_challenge(text)

    if blocked:
        result = {"status": "blocked", "fetch": label, "offers": [], "cheapest": None}
    else:
        offers = parse_offers(text)
        result = {
            "status": "ok" if offers else "no_matches",
            "fetch": label,
            "offers": offers,
            "cheapest": offers[0] if offers else None,
        }

    state = load_json(STATE_PATH, {})
    prev_price = state.get("last_cheapest_price")
    dropped = False

    if write:
        history = load_json(HISTORY_PATH, [])
        history.append({
            "timestamp_iso": now,
            "status": result["status"],
            "fetch": result["fetch"],
            "offer_count": len(result["offers"]),
            "cheapest_price": result["cheapest"]["price_eur"] if result["cheapest"] else None,
            "cheapest_product": result["cheapest"]["name"] if result["cheapest"] else None,
        })
        save_json(HISTORY_PATH, history)

        if result["status"] == "ok":
            new_price = result["cheapest"]["price_eur"]
            if prev_price is not None and new_price < prev_price:
                dropped = True
            state["last_cheapest_price"] = new_price
            state["last_cheapest_product"] = result["cheapest"]["name"]
            state["last_success_iso"] = now
            state["consecutive_blocked"] = 0
        elif blocked:
            state["consecutive_blocked"] = int(state.get("consecutive_blocked", 0)) + 1
        save_json(STATE_PATH, state)

    result.update({
        "timestamp_iso": now,
        "previous_price": prev_price,
        "dropped": dropped,
        "consecutive_blocked": state.get("consecutive_blocked", 0),
    })
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="fetch and report without writing state")
    ap.add_argument("--from-file", help="parse a saved HTML file instead of fetching")
    args = ap.parse_args()

    result = run(write=not args.probe, from_file=args.from_file)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in ("ok", "no_matches") else 2


if __name__ == "__main__":
    sys.exit(main())
