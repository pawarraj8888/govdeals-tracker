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
WINDOW_MINUTES = 15      # tracking-window length; cron should match (every 15 min)
MAX_WINDOWS   = 1000
MAX_CLOSURES  = 80000                       # rolling cap so the file stays loadable
ROWS          = 200      # rows per page requested
MAX_PAGES     = 1000     # safety cap
SLEEP         = 0.20     # politeness delay between pages

# Per-asset detail (bid box) endpoint — confirms true outcome of a closed lot.
# Path: .../bids/bidbox/GD/{assetId}/{accountId}/1  (id we store is "account-asset")
BIDBOX_URL    = "https://maestro.lqdt1.com/bids/bidbox/GD/{asset}/{account}/1"
SLEEP_DETAIL  = 0.05     # delay between detail confirmations
CONFIRM_CAP   = 4000     # max lots to confirm per window (protects catch-up runs)

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
        "high_bidder": bool(it.get("highBidder")),
        "end_date": end,
        "has_reserve": bool(it.get("hasReservePrice")),
        "reserve_not_met": bool(it.get("isReserveNotMet")),
    }


def fetch_all_active():
    session = requests.Session()
    items, page, complete = {}, 1, True
    while page <= MAX_PAGES:
        results = _search_page(session, page)
        if results is None:        # hard failure mid-pagination -> set incomplete
            complete = False
            break
        if not results:            # empty page = clean end of results
            break
        for it in results:
            n = normalize(it)
            items[n["id"]] = n
        if page % 20 == 0:
            print(f"  page {page:>4} ... {len(items):>6} items so far")
        page += 1
        time.sleep(SLEEP)
    print(f"  pulled {len(items)} active listings across "
          f"{len({i['category'] for i in items.values()})} categories"
          f"{'' if complete else '  [INCOMPLETE]'}")
    return {"captured_at": now_utc().isoformat(), "items": items}, complete


def parse_end(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def has_bids(item):
    """True if real bidding occurred: current bid rose above its first-seen level,
    or a high bidder is present. bidCount is unreliable from the list API (always null)."""
    cur = item.get("current_bid", 0) or 0
    start = item.get("start_bid", cur)
    peak = item.get("peak_bid", cur)
    return peak > start or bool(item.get("high_bidder"))


def classify(item, when):
    """Why did this listing leave the active set? Uses its last-seen state.
    The list API exposes no bid count, bidder, or sold flag, so:
      - reserve auctions: reserve met at close => sold (near ground truth)
      - no-reserve auctions: sold if the bid visibly rose (movement proxy).
        Single bids at the opening price don't move currentBid, so no-reserve
        sales are a known undercount; reserve auctions are reliable."""
    end = parse_end(item.get("end_date"))
    if end and end.tzinfo is None:
        end = end.replace(tzinfo=dt.timezone.utc)
    if end and end > when + dt.timedelta(minutes=WINDOW_MINUTES):
        return "pulled"
    if item.get("has_reserve"):
        return "no_sale" if item.get("reserve_not_met") else "sold"
    return "sold" if has_bids(item) else "no_sale"


def fetch_bid_detail(session, asset_id, account_id):
    """Hit the per-asset bid-box endpoint for ground-truth outcome.
    Returns the JSON dict, {"_removed": True} on 404, or None on failure."""
    url = BIDBOX_URL.format(asset=asset_id, account=account_id)
    headers = dict(BASE_HEADERS)
    headers["x-api-correlation-id"] = str(uuid.uuid4())
    for attempt in range(2):
        try:
            r = session.get(url, headers=headers, timeout=30)
            if r.status_code == 404:
                return {"_removed": True}
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == 0:
                time.sleep(1)
                continue
            return None


def _ended_from_detail(detail, when):
    """True if the auction has actually ended, per its detail record."""
    end = parse_end(detail.get("assetAuctionEndDateUTC") or detail.get("assetAuctionEndDate"))
    if end is not None:
        if end.tzinfo is None:
            end = end.replace(tzinfo=dt.timezone.utc)
        return end <= when
    # fallback: timeRemaining like "0:0:-3:-30" — any negative field means ended
    tr = detail.get("timeRemaining") or ""
    try:
        return any(int(p) < 0 for p in tr.split(":") if p.strip())
    except Exception:
        return False


def confirm_outcome(detail, item, when):
    """Turn a detail record into a true outcome. Falls back to inference when
    the detail call failed (detail is None).
    Returns {status, price, bid_count, confirmed, still_active}."""
    if detail is None:                                  # call failed -> infer
        st = classify(item, when)
        return {"status": st, "price": item.get("current_bid", 0) if st == "sold" else 0,
                "bid_count": item.get("bid_count", 0), "confirmed": False, "still_active": False}
    if detail.get("_removed"):                          # 404 -> withdrawn
        return {"status": "pulled", "price": 0, "bid_count": item.get("bid_count", 0),
                "confirmed": True, "still_active": False}
    if not _ended_from_detail(detail, when):            # still live -> pagination miss
        return {"status": "active", "price": float(detail.get("currentBid") or item.get("current_bid", 0)),
                "bid_count": detail.get("bidCount") or 0, "confirmed": True, "still_active": True}
    bids = detail.get("bidCount") or 0
    has_bidder = detail.get("highBidder") is not None
    reserve = bool(detail.get("hasReservePrice"))
    reserve_not_met = bool(detail.get("isReserveNotMet"))
    sold = bids >= 1 and has_bidder and (not reserve or not reserve_not_met)
    price = float(detail.get("currentBid") or 0)
    return {"status": "sold" if sold else "no_sale", "price": price if sold else 0,
            "bid_count": bids, "confirmed": True, "still_active": False}


def confirm_closures(prev_items, curr, when):
    """For every lot that left the active set, confirm its real outcome via the
    detail endpoint. Items still live are re-added to the active snapshot
    (they were just pagination misses). Returns (confirmed, reactivated, n_conf)."""
    gone = list(set(prev_items) - set(curr["items"]))
    confirmed, reactivated, n_conf = {}, 0, 0
    if not gone:
        return confirmed, reactivated, n_conf
    session = requests.Session()
    print(f"  confirming {len(gone)} closed candidates via detail API ...")
    for n, pid in enumerate(gone[:CONFIRM_CAP], 1):
        item = prev_items[pid]
        parts = str(pid).split("-")           # id == "account-asset"
        if len(parts) != 2:
            confirmed[pid] = confirm_outcome(None, item, when)
            continue
        account_id, asset_id = parts[0], parts[1]
        detail = fetch_bid_detail(session, asset_id, account_id)
        out = confirm_outcome(detail, item, when)
        if out["still_active"]:               # not really closed — restore to active
            restored = dict(item)
            restored["current_bid"] = out["price"]
            curr["items"][pid] = restored
            reactivated += 1
        else:
            confirmed[pid] = out
            if out["confirmed"]:
                n_conf += 1
        if n % 100 == 0:
            print(f"    {n}/{min(len(gone), CONFIRM_CAP)} confirmed")
        time.sleep(SLEEP_DETAIL)
    for pid in gone[CONFIRM_CAP:]:            # overflow -> infer (rare)
        item = prev_items[pid]
        confirmed[pid] = confirm_outcome(None, item, when)
    return confirmed, reactivated, n_conf


def diff(prev, curr, confirmed):
    when = now_utc()
    p_items = prev.get("items", {}) if prev else {}
    c_items = curr["items"]

    cats = {i["category"] for i in c_items.values()} | {i["category"] for i in p_items.values()}
    keys = ["sold", "no_sale", "pulled", "new", "active_end", "sold_value", "active_value"]
    by_cat = {c: {k: 0 for k in keys} for c in cats}

    for cid, it in c_items.items():
        b = by_cat[it["category"]]
        b["active_end"] += 1
        b["active_value"] += int(it.get("current_bid") or 0)
        if cid not in p_items:
            b["new"] += 1

    for pid, out in confirmed.items():
        it = p_items.get(pid)
        if it is None:
            continue
        b = by_cat[it["category"]]
        st = out["status"]
        if st not in ("sold", "no_sale", "pulled"):
            continue
        b[st] += 1
        if st == "sold":
            b["sold_value"] += int(out.get("price") or 0)

    totals = {k: sum(by_cat[c][k] for c in by_cat) for k in keys}
    return by_cat, totals, sorted(cats)


def build_closures(prev, curr, when, confirmed):
    """Item-level records for every confirmed close this window."""
    p_items = prev.get("items", {}) if prev else {}
    recs = []
    for pid, out in confirmed.items():
        it = p_items.get(pid)
        if it is None or out["status"] not in ("sold", "no_sale", "pulled"):
            continue
        recs.append({
            "id": pid,
            "title": it.get("title", ""),
            "category": it.get("category", "Uncategorized"),
            "current_bid": out["price"] if out["status"] == "sold" else it.get("current_bid", 0),
            "bid_count": out.get("bid_count", 0),
            "had_bids": (out.get("bid_count", 0) or 0) > 0,
            "end_date": it.get("end_date"),
            "has_reserve": it.get("has_reserve", False),
            "reserve_not_met": it.get("reserve_not_met", False),
            "status": out["status"],
            "confirmed": out.get("confirmed", False),
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
    curr, complete = fetch_all_active()
    if not curr["items"]:
        print("No items pulled — aborting without overwriting good data.")
        return
    if not complete:
        print("Fetch was INCOMPLETE (a page failed mid-pagination). Skipping this "
              "window so missing items aren't mis-counted as pulled. State untouched.")
        return
    if prev and prev.get("items") and len(curr["items"]) < 0.6 * len(prev["items"]):
        print(f"Only fetched {len(curr['items'])} vs previous {len(prev['items'])} "
              f"(<60%) — likely a partial pull. Skipping window. State untouched.")
        return

    # Carry each item's first-seen bid forward so we can detect real bidding
    # (current bid rising above its starting level). bidCount is always null here.
    prev_items = prev.get("items", {}) if prev else {}
    for iid, it in curr["items"].items():
        pit = prev_items.get(iid)
        it["start_bid"] = pit.get("start_bid", pit.get("current_bid", it["current_bid"])) if pit else it["current_bid"]
        it["peak_bid"] = max(it["current_bid"], pit.get("peak_bid", pit.get("current_bid", 0))) if pit else it["current_bid"]

    hist = load(HISTORY_FILE, {"categories": [], "window_hours": WINDOW_MINUTES / 60,
                               "window_minutes": WINDOW_MINUTES, "windows": []})
    hist.pop("_note", None)                      # drop the sample marker
    hist["windows"] = [w for w in hist.get("windows", []) if "by_category" in w]

    cats = sorted({i["category"] for i in curr["items"].values()})
    closures = load(CLOSURES_FILE, {"last_updated": None, "closures": []})
    if prev is None:
        print("First run — baseline snapshot saved, no window emitted yet.")
    else:
        when = now_utc()
        confirmed, reactivated, n_conf = confirm_closures(prev_items, curr, when)
        by_cat, totals, cats = diff(prev, curr, confirmed)
        hist["windows"].append({
            "window_start": prev["captured_at"],
            "window_end":   when.isoformat(),
            "by_category":  by_cat,
            "totals":       totals,
        })
        hist["windows"] = hist["windows"][-MAX_WINDOWS:]
        recs = build_closures(prev, curr, when, confirmed)
        closures["closures"].extend(recs)
        closures["closures"] = closures["closures"][-MAX_CLOSURES:]
        if reactivated:
            print(f"  re-activated {reactivated} items still live (pagination misses)")
        print(f"Window closed: {totals['sold']} sold / {totals['no_sale']} no-sale "
              f"/ {totals['pulled']} pulled / {totals['new']} new "
              f"({n_conf} confirmed via detail, GMV ${totals['sold_value']:,})")

    # union of categories ever seen, so the dashboard keeps a stable list
    hist["categories"] = sorted(set(hist.get("categories", [])) | set(cats))
    hist["window_hours"] = WINDOW_MINUTES / 60
    hist["window_minutes"] = WINDOW_MINUTES
    hist["last_updated"] = now_utc().isoformat()

    HISTORY_FILE.write_text(json.dumps(hist, indent=2))
    SNAPSHOT_FILE.write_text(json.dumps(curr))
    closures["last_updated"] = now_utc().isoformat()
    CLOSURES_FILE.write_text(json.dumps(closures))
    print(f"Wrote history.json + snapshot.json + closures.json "
          f"({len(closures['closures'])} closures logged)")


if __name__ == "__main__":
    main()