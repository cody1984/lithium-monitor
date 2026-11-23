# Lithium Market Monitor

One dashboard page, a daily data job, and a Google Sheet for the numbers that have no free feed.

## Files in the repo root

| File | What it is |
|---|---|
| lithium_monitor.html | The dashboard (single file, no other scripts) |
| fetch_data.py | Daily job: FX, GFEX bar, futures curve, receipts, proxy spot, LTR/PLS |
| .github/workflows/update.yml | Schedules the job (weekdays 17:37 AWST). Upload update.yml into that folder |
| market_snapshot.json | Job output; also the dashboard's fallback copy |
| inventory_data.json, demand_gwh.json | Fallback copies of the manual tables (used if the sheet is unreachable) |
| sheet_config.json | Holds your Google Sheet ID |
| market_daily.csv, futures_curve.csv | Created by the job |

Delete old files if present: inventory_module.js, market_dynamics_module.js.

## One-time setup

1. Repo public, GitHub Pages on (Settings > Pages > main / root), Actions enabled.
2. Run the workflow once from the Actions tab.
3. Google Sheet: File > Import > Upload lithium_sheet_template.xlsx > Replace spreadsheet. Keep all tab names. Share > Anyone with the link > Viewer.
4. Paste the sheet URL into the dashboard (Data update > Google Sheet box > Load sheet), or put the ID in sheet_config.json as {"sheet_id": "..."}.

## Rules

- Dates are YYYY-MM-DD text; months are YYYY-MM. Leave unknowns empty. Fill source every time.
- A tab with at least one valid row replaces the repo copy of that table; smm_spot merges by date.
- The sheet is readable by anyone with the link: public market data only.
