#!/usr/bin/env python3
"""
GovDeals disposition tracker.

Every run it:
  1. Pulls the CURRENT set of active listings per category (the "snapshot").
  2. Diffs against the PREVIOUS snapshot.
  3. Items that disappeared = auctions that closed. Each is classified
     SOLD / NO_SALE / PULLED using its last-seen bid count and end time.
  4. Appends one window record (per category + totals) to history.json.

Designed to be run on a 2-hour cron (see .github/workflows/track.yml).
The data SOURCE is swappable: Apify (recommended, reuses your existing
GovDeals actor) or a direct API call you capture from DevTools.
"""

import os
import json
import time
import datetime as dt
from pathlib import Path

# ----------------------------------------------------------------------------
# Categories to track. Use the labels you want shown on the dashboard.
# For Apify mode, map each label to the GovDeals category code (numbers you
# see in the URL when you click a category). For direct mode you can pass the
# label as a search/category filter. Edit freely.
# ----------------------------------------------------------------------------
CATEGORIES = {
    "Vehicles & Watercraft":      "2",
    "Heavy Equipment":            "3",
    "Computers & Electronics":    "10",
    "Office Furniture":           "8",
    "Industrial Machinery":       "5",
    "Tools & Shop Equipment":     "7",
    "Facility Support Equipment": "12",
    "Lab & Medical":              "11",
    "Apparel & Lots":             "9",
    "Real Estate":                "1",
    "Agricultural":               "4",
    "Public Safety & Fire":       "6",
}

SNAPSHOT_FILE = Path("snapshot.json")
HISTORY_FILE  = Path("history.json")
WINDOW_HOURS  = 2
MAX_WINDOWS   = 500          # keep history bounded
SOURCE        = os.getenv("SOURCE", "apify").lower()   # "apify" | "direct"


# ============================================================================
# DATA SOURCE  — return a list of normalized item dicts for one category.
# Each item MUST have: id, title, current_bid, bid_count, end_date (ISO str).
# ============================================================================

def fetch_category_apify(label, code):
    """
    Recommended: reuse your existing Apify GovDeals actor.
    Set secrets APIFY_TOKEN and APIFY_ACTOR (e.g. parseforge~govdeals-scraper).
    The actor already returns title/current bid/bid count/end date per listing,
    so we just normalize its output.
    """
    import requests
    token = os.environ["APIFY_TOKEN"]
    actor = os.getenv("APIFY_ACTOR", "parseforge~govdeals-scraper")
    url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token={token}"
    payload = {"category": code, "maxItems": 1000}   # match your actor's input schema
    r = requests.post(url, json=payload, timeout=300)
    r.raise_for_status()
    items = r.json()
    out = []
    for it in items:
        out.append({
            "id":          str(it.get("id") or it.get("assetId") or it.get("itemId")),
            "title":       it.get("title", ""),
            "current_bid": float(it.get("currentBid") or it.get("current_bid") or 0),
            "bid_count":   int(it.get("bidCount") or it.get("bids") or 0),
            "end_date":    it.get("endDate") or it.get("auctionEnd") or it.get("end_date"),
        })
    return [x for x in out if x["id"] and x["id"] != "None"]


def fetch_category_direct(label, code):
    """
    Direct mode. Capture the real request in your browser:
      DevTools > Network > filter 'search' or 'buscar' while browsing a category,
      copy the request URL + JSON body, and paste the shape below.
    GovDeals is a SPA backed by a JSON search endpoint that paginates, so loop
    pages until you've collected the full active set for the category.
    """
    import requests
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (research; disposition-tracker)",
        "Accept": "application/json",
    })
    out, page, page_size = [], 1, 96
    while True:
        # >>> REPLACE this block with the exact endpoint/params you captured <<<
        resp = session.get(
            "https://www.govdeals.com/api/search",     # placeholder path
            params={"category": code, "status": "active",
                    "page": page, "pageSize": page_size},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("results") or data.get("items") or []
        for it in rows:
            out.append({
                "id":          str(it.get("id")),
                "title":       it.get("title", ""),
                "current_bid": float(it.get("currentBid", 0) or 0),
                "bid_count":   int(it.get("bidCount", 0) or 0),
                "end_date":    it.get("endDate"),
            })
        if len(rows) < page_size:
            break
        page += 1
        time.sleep(1.0)   # be polite
    return out


def fetch_category(label, code):
    if SOURCE == "direct":
        return fetch_category_direct(label, code)
    return fetch_category_apify(label, code)


# ============================================================================
# SNAPSHOT + CLASSIFICATION
# ============================================================================

def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def take_snapshot():
    captured = now_utc()
    items = {}
    for label, code in CATEGORIES.items():
        try:
            rows = fetch_category(label, code)
        except Exception as e:
            print(f"  ! {label}: fetch failed ({e}) — skipping this category")
            rows = []
        for it in rows:
            it["category"] = label
            items[it["id"]] = it
        print(f"  {label:<28} {len(rows):>5} active")
        time.sleep(0.5)
    return {"captured_at": captured.isoformat(), "items": items}


def parse_end(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def classify(item, when):
    """Why did this item leave the active set? Uses last-seen state."""
    end = parse_end(item.get("end_date"))
    bids = item.get("bid_count", 0)
    # vanished clearly before its scheduled end -> seller pulled / withdrawn
    if end and end > when + dt.timedelta(hours=WINDOW_HOURS):
        return "pulled"
    # closed at/after end: bids decide sold vs no-sale
    return "sold" if bids and bids > 0 else "no_sale"


def diff(prev, curr):
    """Per-category counts of sold / no_sale / pulled / new / active."""
    when = now_utc()
    p_items = prev.get("items", {}) if prev else {}
    c_items = curr["items"]
    p_ids, c_ids = set(p_items), set(c_items)

    by_cat = {label: {"sold": 0, "no_sale": 0, "pulled": 0, "new": 0, "active_end": 0}
              for label in CATEGORIES}

    for cid in c_ids:
        cat = c_items[cid]["category"]
        if cat in by_cat:
            by_cat[cat]["active_end"] += 1
            if cid not in p_ids:
                by_cat[cat]["new"] += 1

    for pid in (p_ids - c_ids):              # disappeared = closed
        it = p_items[pid]
        cat = it.get("category")
        if cat in by_cat:
            by_cat[cat][classify(it, when)] += 1

    totals = {k: sum(by_cat[c][k] for c in by_cat)
              for k in ["sold", "no_sale", "pulled", "new", "active_end"]}
    return by_cat, totals


def load(path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def main():
    print(f"GovDeals tracker · source={SOURCE} · {now_utc().isoformat()}")
    prev = load(SNAPSHOT_FILE, None)
    curr = take_snapshot()

    hist = load(HISTORY_FILE, {"categories": list(CATEGORIES),
                               "window_hours": WINDOW_HOURS, "windows": []})

    if prev is None:
        print("First run — baseline snapshot saved, no window emitted yet.")
    else:
        by_cat, totals = diff(prev, curr)
        w_end = now_utc()
        hist["windows"].append({
            "window_start": prev["captured_at"],
            "window_end":   w_end.isoformat(),
            "by_category":  by_cat,
            "totals":       totals,
        })
        hist["windows"] = hist["windows"][-MAX_WINDOWS:]
        print(f"Window closed: {totals['sold']} sold / {totals['no_sale']} no-sale "
              f"/ {totals['pulled']} pulled / {totals['new']} new")

    hist["categories"] = list(CATEGORIES)
    hist["last_updated"] = now_utc().isoformat()
    HISTORY_FILE.write_text(json.dumps(hist, indent=2))
    SNAPSHOT_FILE.write_text(json.dumps(curr))
    print("Wrote history.json + snapshot.json")


if __name__ == "__main__":
    main()
