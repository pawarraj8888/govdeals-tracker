#!/usr/bin/env python3
"""One-shot probe of GovDeals search/list. Prints the response shape so we can
map fields correctly. Run locally, paste the output back."""
import json, uuid, requests

URL = "https://maestro.lqdt1.com/search/list"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.govdeals.com",
    "Referer": "https://www.govdeals.com/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Ocp-Apim-Subscription-Key": "cf620d1d8f904b5797507dc5fd1fdb80",
    "x-api-key": "af93060f-337e-428c-87b8-c74b5837d6cd",
    "x-user-id": "-1",
    "x-api-correlation-id": str(uuid.uuid4()),
    "x-ecom-session-id": str(uuid.uuid4()),
    "x-referer": "https://www.govdeals.com/en/facility-support-equipment",
    "x-user-timezone": "America/New_York",
}
BODY = {
    "categoryIds": "", "businessId": "GD", "searchText": "*", "isQAL": False,
    "locationId": None, "model": "", "makebrand": "", "auctionTypeId": None,
    "page": 1, "displayRows": 5, "sortField": "bestfit", "sortOrder": "desc",
    "sessionId": str(uuid.uuid4()), "requestType": "search",
    "responseStyle": "fullResponse", "facets": ["categoryName"],
    "facetsFilter": [], "timeType": "", "sellerTypeId": None, "accountIds": [],
}

r = requests.post(URL, headers=HEADERS, json=BODY, timeout=60)
print("HTTP", r.status_code)
try:
    data = r.json()
except Exception:
    print("NOT JSON. First 500 chars:\n", r.text[:500]); raise SystemExit

print("\nTOP-LEVEL KEYS:", list(data.keys()))

# find the list of items wherever it lives
items = None
for k, v in data.items():
    if isinstance(v, list) and v and isinstance(v[0], dict):
        items, items_key = v, k
        break
    if isinstance(v, dict):
        for k2, v2 in v.items():
            if isinstance(v2, list) and v2 and isinstance(v2[0], dict):
                items, items_key = v2, f"{k}.{k2}"
                break
    if items:
        break

if not items:
    print("\nNo item list found. Full response (truncated):")
    print(json.dumps(data, indent=2)[:2000]); raise SystemExit

print("ITEM LIST AT:", items_key, "| count this page:", len(items))
print("\nFIRST ITEM KEYS:", list(items[0].keys()))
print("\nFIRST ITEM:")
print(json.dumps(items[0], indent=2)[:2500])
