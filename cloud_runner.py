#!/usr/bin/env python3
"""Cloud entry point for the Alza MacBook Pro monitor.

Runs one check, records history, pushes a notification on a price drop, and
rewrites README.md as a dashboard that GitHub renders (so no hosting needed).

Fetch strategy is chosen by environment, not by editing code:
  * SCRAPER_API_KEY set -> route through ScraperAPI (handles the bot challenge)
  * otherwise           -> direct fetch (free, but often challenged from cloud IPs)

Environment variables:
  SCRAPER_API_KEY  optional; enables the scraping-API path
  NTFY_TOPIC       optional; ntfy.sh topic for phone push notifications
  NTFY_SERVER      optional; defaults to https://ntfy.sh
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from fetch_and_update import (
    HISTORY_PATH,
    STATE_PATH,
    URL,
    fetch as direct_fetch,
    is_challenge,
    load_json,
    parse_offers,
    save_json,
)

SCRAPER_KEY = os.environ.get("SCRAPER_API_KEY", "").strip()
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

MAX_HISTORY_ROWS = 40


def fetch_via_scraper(url):
    """Fetch through ScraperAPI. Returns (label, body_bytes)."""
    endpoint = "https://api.scraperapi.com/?" + urllib.parse.urlencode({
        "api_key": SCRAPER_KEY,
        "url": url,
        "render": "true",  # execute JS so the challenge can resolve
    })
    req = urllib.request.Request(endpoint, headers={"Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return "scraperapi-%s" % resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:
            body = b""
        return "scraperapi-http-%d" % exc.code, body
    except Exception as exc:
        return "scraperapi-error-%s" % type(exc).__name__, b""


def notify(title, message, click=None, priority="default", tags="chart_with_downwards_trend"):
    """Send a push notification via ntfy.sh. Never raises."""
    if not NTFY_TOPIC:
        print("[notify] NTFY_TOPIC not set; skipping push")
        return False
    headers = {
        "Title": title.encode("utf-8").decode("latin-1", "ignore"),
        "Priority": priority,
        "Tags": tags,
    }
    if click:
        headers["Click"] = click
    req = urllib.request.Request(
        "%s/%s" % (NTFY_SERVER, NTFY_TOPIC),
        data=message.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print("[notify] sent, status", resp.status)
            return True
    except Exception as exc:
        print("[notify] failed:", type(exc).__name__, exc)
        return False


def eur(value):
    return "{:,.0f} €".format(value).replace(",", " ")


def build_readme(offers, status, fetch_label, state, history):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = ["# MacBook Watch — Alza.sk (≥36 GB RAM)", ""]

    blocked_streak = int(state.get("consecutive_blocked", 0))
    if status == "ok":
        lines.append("**Status:** ✅ live data · checked %s" % now)
    elif status == "no_matches":
        lines.append("**Status:** ⚠️ page fetched but no qualifying models found · %s" % now)
    else:
        lines.append(
            "**Status:** 🚫 blocked by Alza's bot protection (`%s`) · %s" % (fetch_label, now)
        )
        lines.append("")
        lines.append(
            "> Prices below are from the last successful check, **not** current. "
            "Blocked checks in a row: **%d**." % blocked_streak
        )
    lines.append("")

    if offers:
        cheapest = offers[0]
        lines += [
            "## Cheapest right now",
            "",
            "### %s — **%s**" % (cheapest["name"], eur(cheapest["price_eur"])),
            "",
            "[Open on Alza](%s)" % cheapest["url"],
            "",
            "## All qualifying offers",
            "",
            "| Price | RAM | Model |",
            "|---|---|---|",
        ]
        for o in offers:
            lines.append("| %s | %d GB | [%s](%s) |" % (
                eur(o["price_eur"]), o["ram_gb"], o["name"], o["url"]))
        lines.append("")
    else:
        last_price = state.get("last_cheapest_price")
        if last_price:
            lines += [
                "## Last known cheapest",
                "",
                "**%s** — %s" % (state.get("last_cheapest_product", "?"), eur(last_price)),
                "",
            ]

    rows = [h for h in history if h.get("cheapest_price")][-MAX_HISTORY_ROWS:]
    if rows:
        lines += ["## Price history (cheapest qualifying model)", "",
                  "| Checked (UTC) | Cheapest | Model |", "|---|---|---|"]
        for h in reversed(rows):
            ts = h["timestamp_iso"][:16].replace("T", " ")
            lines.append("| %s | %s | %s |" % (
                ts, eur(h["cheapest_price"]), (h.get("cheapest_product") or "")[:60]))
        lines.append("")

    total = len(history)
    blocked_total = sum(1 for h in history if h.get("status") == "blocked")
    lines += [
        "---",
        "",
        "_Checks recorded: %d · blocked: %d · fetch mode: %s_" % (
            total, blocked_total, "ScraperAPI" if SCRAPER_KEY else "direct"),
        "",
        "_Updated automatically by GitHub Actions._",
    ]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-file", help="parse a saved HTML file (local testing)")
    ap.add_argument("--dry-run", action="store_true", help="don't write state or notify")
    args = ap.parse_args()

    now = datetime.now(timezone.utc).isoformat()

    if args.from_file:
        with open(args.from_file, "rb") as fh:
            body = fh.read()
        label = "file"
    elif SCRAPER_KEY:
        label, body = fetch_via_scraper(URL)
    else:
        label, body = direct_fetch(URL)

    text = body.decode("utf-8", errors="replace")
    blocked = (not text) or ("http-" in label) or ("error-" in label) or is_challenge(text)

    offers = [] if blocked else parse_offers(text)
    status = "blocked" if blocked else ("ok" if offers else "no_matches")
    print("[fetch] label=%s bytes=%d status=%s offers=%d" % (label, len(text), status, len(offers)))

    state = load_json(STATE_PATH, {})
    history = load_json(HISTORY_PATH, [])
    prev_price = state.get("last_cheapest_price")
    dropped = False

    if status == "ok":
        new_price = offers[0]["price_eur"]
        if prev_price is not None and new_price < prev_price:
            dropped = True
        state["last_cheapest_price"] = new_price
        state["last_cheapest_product"] = offers[0]["name"]
        state["last_cheapest_url"] = offers[0]["url"]
        state["last_success_iso"] = now
        state["consecutive_blocked"] = 0
    elif blocked:
        state["consecutive_blocked"] = int(state.get("consecutive_blocked", 0)) + 1

    history.append({
        "timestamp_iso": now,
        "status": status,
        "fetch": label,
        "offer_count": len(offers),
        "cheapest_price": offers[0]["price_eur"] if offers else None,
        "cheapest_product": offers[0]["name"] if offers else None,
    })

    # Offers shown when blocked fall back to last known, clearly labelled in README.
    display_offers = offers
    readme = build_readme(display_offers, status, label, state, history)

    if args.dry_run:
        print("--- README preview ---")
        print(readme)
        print("--- would notify:", dropped)
        return 0

    save_json(HISTORY_PATH, history)
    save_json(STATE_PATH, state)
    with open(os.path.join(os.path.dirname(STATE_PATH), "README.md"), "w", encoding="utf-8") as fh:
        fh.write(readme)

    if dropped:
        saved = prev_price - state["last_cheapest_price"]
        notify(
            "MacBook Pro price drop: %s" % eur(state["last_cheapest_price"]),
            "%s\nwas %s → now %s (−%s)" % (
                state["last_cheapest_product"], eur(prev_price),
                eur(state["last_cheapest_price"]), eur(saved)),
            click=state.get("last_cheapest_url"),
            priority="high",
        )
    elif blocked and state.get("consecutive_blocked") == 6:
        # One heads-up when it's been failing for hours, not every cycle.
        notify(
            "MacBook monitor is blocked",
            "6 checks in a row were blocked by Alza's bot protection. "
            "Consider adding a SCRAPER_API_KEY secret.",
            priority="low",
            tags="warning",
        )

    return 0 if status in ("ok", "no_matches") else 2


if __name__ == "__main__":
    sys.exit(main())
