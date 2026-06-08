#!/usr/bin/env python3
"""
GovDeals disposition tracker.

Every run:
  1. Pulls the CURRENT active listing set for the whole marketplace
     (one search, paginated) via GovDeals' own search/list API.
  2. Diffs against the PREVIOUS snapshot. Items that disappeared = auctions
     that closed.
  3. Classifies each close as SOLD / NO_SALE / PULLED using last-seen bid
     count, reserve status, and scheduled end time.
  4. Buckets everything by each item's own categoryDescription and appends
     one window record (per category + totals) to history.json.

Runs on a 2-hour cron (.github/workflows/track.yml). No Apify, no API keys
to manage beyond the public client keys GovDeals embeds in its own frontend.
"""

import json
import time
import uuid
import datetime as dt
from pathlib import Path

import requests

SEARCH_URL    = "https://maestro.lqdt1.com/search/list"
SNAPSHOT_FILE = Path("snapshot.json")
HISTORY_FILE  = Path("history.json")
CLOSURES_FILE = Path("closures.json")     # item-level log of every auction that closed
WINDOW_HOURS  = 2
MAX_WINDOWS   = 1000
MAX_CLOSURES  = 80000                       # rolling cap so the file stays loadable
ROWS          = 200      # rows per page requested
MAX_PAGES     = 1000     # safety cap
SLEEP         = 0.20     # politeness delay between pages

# Public client keys baked into the GovDeals frontend (same for every visitor).
# If the API starts returning 401/403, re-grab these from DevTools.
BASE_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.govdeals.com",
    "Referer": "https://www.govdeals.com/",
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/148.0.0.0 Safari/537.36"),
    "Ocp-Apim-Subscription-Key": "cf620d1d8f904b5797507dc5fd1fdb80",
    "x-api-key": "af93060f-337e-428c-87b8-c74b5837d6cd",
    "x-user-id": "-1",
    "x-user-timezone": "America/New_York",
    "x-referer": "https://www.govdeals.com/en/search",
}


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def _search_page(session, page):
    headers = dict(BASE_HEADERS)
    headers["x-api-correlation-id"] = str(uuid.uuid4())
    headers["x-ecom-session-id"] = str(uuid.uuid4())
    body = {
        "categoryIds": "", "businessId": "GD", "searchText": "*", "isQAL": False,
        "locationId": None, "model": "", "makebrand": "", "auctionTypeId": None,
        "page": page, "displayRows": ROWS, "sortField": "bestfit",
        "sortOrder": "desc", "sessionId": str(uuid.uuid4()), "requestType": "search",
        "responseStyle": "fullResponse", "facets": [], "facetsFilter": [],
        "timeType": "", "sellerTypeId": None, "accountIds": [],
    }
    for attempt in range(2):
        try:
            r = session.post(SEARCH_URL, headers=headers, json=body, timeout=90)
            r.raise_for_status()
            return r.json().get("assetSearchResults") or []
        except Exception as e:
            if attempt == 0:
                time.sleep(2)
                continue
            print(f"  ! page {page} failed: {e}")
            return None


def normalize(it):
    aid = it.get("assetId")
    acct = it.get("accountId")
    end = it.get("assetAuctionEndDateUtc") or it.get("assetAuctionEndDate")
    return {
        "id": f"{acct}-{aid}",
        "title": it.get("assetShortDescription") or "",
        "category": it.get("categoryDescription") or it.get("assetCategory") or "Uncategorized",
        "current_bid": float(it.get("currentBid") or 0),
        "bid_count": int(it.get("bidCount") or 0),
        "end_date": end,
        "has_reserve": bool(it.get("hasReservePrice")),
        "reserve_not_met": bool(it.get("isReserveNotMet")),
    }


def fetch_all_active():
    session = requests.Session()
    items, page = {}, 1
    while page <= MAX_PAGES:
        results = _search_page(session, page)
        if results is None:        # hard failure; keep what we have
            break
        if not results:            # empty page = end of results
            break
        for it in results:
            n = normalize(it)
            items[n["id"]] = n
        if page % 20 == 0:
            print(f"  page {page:>4} ... {len(items):>6} items so far")
        page += 1
        time.sleep(SLEEP)
    print(f"  pulled {len(items)} active listings across "
          f"{len({i['category'] for i in items.values()})} categories")
    return {"captured_at": now_utc().isoformat(), "items": items}


def parse_end(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def classify(item, when):
    """Why did this listing leave the active set? Uses its last-seen state."""
    end = parse_end(item.get("end_date"))
    if end and end.tzinfo is None:
        end = end.replace(tzinfo=dt.timezone.utc)
    # vanished well before its scheduled end -> seller pulled / withdrawn
    if end and end > when + dt.timedelta(hours=WINDOW_HOURS):
        return "pulled"
    bids = item.get("bid_count", 0)
    reserve_failed = item.get("has_reserve") and item.get("reserve_not_met")
    if bids > 0 and not reserve_failed:
        return "sold"
    return "no_sale"


def diff(prev, curr):
    when = now_utc()
    p_items = prev.get("items", {}) if prev else {}
    c_items = curr["items"]
    p_ids, c_ids = set(p_items), set(c_items)

    cats = {i["category"] for i in c_items.values()} | {i["category"] for i in p_items.values()}
    keys = ["sold", "no_sale", "pulled", "new", "active_end", "sold_value", "active_value"]
    by_cat = {c: {k: 0 for k in keys} for c in cats}

    for cid, it in c_items.items():
        b = by_cat[it["category"]]
        b["active_end"] += 1
        b["active_value"] += int(it.get("current_bid") or 0)
        if cid not in p_ids:
            b["new"] += 1

    for pid in (p_ids - c_ids):
        it = p_items[pid]
        b = by_cat[it["category"]]
        st = classify(it, when)
        b[st] += 1
        if st == "sold":
            b["sold_value"] += int(it.get("current_bid") or 0)

    totals = {k: sum(by_cat[c][k] for c in by_cat) for k in keys}
    return by_cat, totals, sorted(cats)


def build_closures(prev, curr, when):
    """Item-level records for every listing that left the active set this window."""
    p_items = prev.get("items", {}) if prev else {}
    gone = set(p_items) - set(curr["items"])
    recs = []
    for pid in gone:
        it = p_items[pid]
        recs.append({
            "id": pid,
            "title": it.get("title", ""),
            "category": it.get("category", "Uncategorized"),
            "current_bid": it.get("current_bid", 0),
            "bid_count": it.get("bid_count", 0),
            "end_date": it.get("end_date"),
            "has_reserve": it.get("has_reserve", False),
            "reserve_not_met": it.get("reserve_not_met", False),
            "status": classify(it, when),
            "closed_at": when.isoformat(),
        })
    return recs


def load(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default


def main():
    print(f"GovDeals tracker · {now_utc().isoformat()}")
    prev = load(SNAPSHOT_FILE, None)
    curr = fetch_all_active()
    if not curr["items"]:
        print("No items pulled — aborting without overwriting good data.")
        return

    hist = load(HISTORY_FILE, {"categories": [], "window_hours": WINDOW_HOURS, "windows": []})
    hist.pop("_note", None)                      # drop the sample marker
    hist["windows"] = [w for w in hist.get("windows", []) if "by_category" in w]

    cats = sorted({i["category"] for i in curr["items"].values()})
    closures = load(CLOSURES_FILE, {"last_updated": None, "closures": []})
    if prev is None:
        print("First run — baseline snapshot saved, no window emitted yet.")
    else:
        when = now_utc()
        by_cat, totals, cats = diff(prev, curr)
        hist["windows"].append({
            "window_start": prev["captured_at"],
            "window_end":   when.isoformat(),
            "by_category":  by_cat,
            "totals":       totals,
        })
        hist["windows"] = hist["windows"][-MAX_WINDOWS:]
        recs = build_closures(prev, curr, when)
        closures["closures"].extend(recs)
        closures["closures"] = closures["closures"][-MAX_CLOSURES:]
        print(f"Window closed: {totals['sold']} sold / {totals['no_sale']} no-sale "
              f"/ {totals['pulled']} pulled / {totals['new']} new")

    # union of categories ever seen, so the dashboard keeps a stable list
    hist["categories"] = sorted(set(hist.get("categories", [])) | set(cats))
    hist["window_hours"] = WINDOW_HOURS
    hist["last_updated"] = now_utc().isoformat()

    HISTORY_FILE.write_text(json.dumps(hist, indent=2))
    SNAPSHOT_FILE.write_text(json.dumps(curr))
    closures["last_updated"] = now_utc().isoformat()
    CLOSURES_FILE.write_text(json.dumps(closures))
    print(f"Wrote history.json + snapshot.json + closures.json "
          f"({len(closures['closures'])} closures logged)")


if __name__ == "__main__":
    main()