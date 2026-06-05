# GovDeals Disposition Tracker

Tracks the active listing set on GovDeals **per category, every 2 hours**. GovDeals
removes an auction from search the moment it closes (it never marks it "sold"), so
the tracker treats *disappearance between snapshots* as a close, and classifies each
close as **SOLD / NO-SALE / PULLED** from the item's last-seen bid count and end time.

```
tracker.py  →  snapshot.json (current active set)  +  history.json (per-window counts)
index.html  →  reads history.json, renders the dashboard
GitHub Action (cron 0 */2 * * *)  →  runs tracker, commits the JSON back
GitHub Pages →  serves index.html + history.json publicly
```

## Setup (≈5 min)
1. Push this folder to a new repo.
2. **Settings → Pages →** deploy from branch (root). Your dashboard is the Pages URL.
3. **Settings → Actions → General →** allow read/write for the workflow token.
4. Pick a data source:
   - **Apify (recommended — you already use it):** add repo secret `APIFY_TOKEN`,
     and repo variable `APIFY_ACTOR` (e.g. `parseforge~govdeals-scraper`).
     Confirm the actor's input keys match `fetch_category_apify` in `tracker.py`.
   - **Direct API:** set repo variable `SOURCE=direct`, then in `tracker.py` paste the
     real search endpoint into `fetch_category_direct` (grab it from DevTools → Network
     while browsing a category — it's a paginated JSON call).
5. First scheduled run = baseline (no window). Every run after emits one 2-hour window.

## Notes
- `index.html` ships with embedded **sample** data so it renders before any run.
  The first live run overwrites `history.json` and the SAMPLE badge flips to LIVE.
- "Sold" = auction closed with bids (an upper bound on cleared sales). Capturing
  bid_count per snapshot is what separates real sales from no-bid expirations.
- Edit `CATEGORIES` in `tracker.py` to add/remove categories or fix the codes.
- Throttle and respect GovDeals' terms; the Apify path keeps you on a maintained scraper.
