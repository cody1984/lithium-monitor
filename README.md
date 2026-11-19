# Lithium Supply-Chain Monitor - cloud pipeline

market_daily.csv and market_snapshot.json are produced by a scheduled GitHub
Actions job, not by anything running locally.

## One-time setup (about 10 minutes)

1. Create a GitHub repository (public = free unlimited Actions minutes;
   private works too within the 2,000 min/month free allowance).
2. Upload these four files, keeping names exact:
   - lithium_monitor.html
   - market_snapshot.json   (bundled starting data - the job merges into it)
   - fetch_data.py
   - .github/workflows/update.yml   (rename update.yml to this path)
3. Get a free MetalCharts API key (no card) at the MetalCharts API console,
   then add it as a repository secret named METALCHARTS_KEY
   (Settings -> Secrets and variables -> Actions -> New repository secret).
   Optional: the job runs fine without it; you just lose the indicative
   USD-converted quote.
4. Enable GitHub Pages: Settings -> Pages -> Deploy from a branch -> main / root.
   The dashboard is then live at:
   https://YOURNAME.github.io/REPO/lithium_monitor.html
5. Actions tab -> "Update lithium data" -> Run workflow, to test once.

## What runs automatically

Weekdays 20:30 AWST (17:30 Beijing, after GFEX settlement), the job:

- pulls the LC contract daily bar (close, volume, OI) via AKShare
- pulls the 99qh lithium carbonate spot trend via AKShare (flagged PROXY for SMM)
- optionally pulls the MetalCharts live lithium quote (free tier: 200 calls/month)
- pulls the ECB USD/CNY reference rate (keyless)
- merges into market_snapshot.json and writes market_daily.csv
- commits both to the repo

Opening the dashboard on GitHub Pages auto-fetches the latest snapshot.

## What stays manual (by design)

- GFEX registered warehouse receipts: no free automated feed. Update
  occasionally from the X transcriptions or the GFEX Chinese bulletin, via the
  dashboard import (paste rows into a market_daily.csv with the gfex_receipts
  column, or edit market_snapshot.json and commit).
- SGX LICF: delayed/thin prints; update occasionally by hand.
- Contract roll: when LC2701 approaches delivery, change GFEX_CONTRACT in
  .github/workflows/update.yml (e.g. to LC2705).

## Notes

- AKShare scrapes public Chinese sites; endpoints can break. A failed feed
  keeps its last values and the run still commits - check the Actions log.
- Free-tier MetalCharts usage is ~30 calls/month at one run per weekday.
- If the repo is public, give MetalCharts a visible credit link on any public
  display of their data (free-plan condition).
