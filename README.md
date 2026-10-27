# Kalshi NBA Props

Self-updating dashboard of every open Kalshi NBA player prop: bid, ask, spread, top-of-book size, order book depth near mid, volume, intraday movement, and a prop-implied DraftKings projection for each player. Runs on GitHub Actions every 15 minutes and publishes to GitHub Pages. No server, no API keys, no manual steps after setup.

## Setup (one time)

1. Create a **public** repo and push these files to `main`.
2. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
3. **Actions tab → "Update Kalshi props" → Run workflow** to kick off the first build.
4. The site is at `https://<user>.github.io/<repo>/`.

The schedule takes over from there.

## What runs, and when

- **Schedule.** `.github/workflows/update.yml` fires at :04, :19, :34 and :49, from 7am to midnight ET.
  - GitHub can delay scheduled runs by 5 to 20 minutes under load.
  - Run it by hand any time from the Actions tab.
- **Pull.** Each run calls `scripts/pull.py`, which uses only the Python standard library. It:
  1. Pulls every open market in the Kalshi NBA player series: points, rebounds, assists, threes, steals, blocks, double-double, triple-double, PRA, P+R, P+A, R+A, and steals+blocks.
  2. Pulls batch order books and measures dollar depth within 5¢ of mid on each side.
  3. Pulls tip times from ESPN's public scoreboard, and this season's and last season's player totals from ESPN's public stats endpoint.
  4. Pulls DraftKings classic salaries for each slate date.
  5. Fits projections and writes JSON.
- **Tip lock.** Once a game tips, its markets stop feeding history and its players' projections lock at the last pregame values. In-game prices never contaminate the closing line.
- **Day to day.** At 6am ET the previous slate rolls into `archive/YYYY-MM-DD.json`, committed to the repo. That file holds the open and close mids for every market and the final pregame projections. The dashboard date menu lists live dates and archived finals.
- **Intraday history.** Kept per live date in `data/history/` on the Pages site. Each run reads the previous history from the live site, appends, and redeploys, so the repo does not grow every 15 minutes. Only one small archive commit lands per game day.

## Dashboard

- **Markets.**
  - Every rung with bid, ask, mid, spread, size at best, depth, volume, 24h volume, and mid change since the first snapshot.
  - Spread color: green at 3¢ or less, yellow at 4 to 8¢, red wider.
  - Click a row for its intraday bid/ask chart and the full ladder.
- **Projections.**
  - Prop-implied stat means and DK points, value per $1k, and DD/TD probabilities.
  - Tier and flags; gray values are defaults.
  - Click a player for the DK projection over the day and every ladder.
- **Movers.** The largest mid moves since the morning.
- **Filters.** Date, game, stat, player/team search, max spread, min volume, and two-sided only.
- **Export.** Writes the current view to CSV. The projections export includes the DK player ID.

The page refetches data every 5 minutes on its own.

## Projection method

- **Fit.** Each stat mean is fit to every Kalshi rung with a spread of 20¢ or less, weighted by tightness.
  - The fit uses a negative binomial with variance = k × mean.
  - k is 2.2 for points, 1.3 for rebounds and assists, 1.25 for threes, 1.1 for steals, and 1.3 for blocks.
- **Combo fill.** A missing single stat is backed out of a combo market when possible, for example rebounds = P+R minus points.
- **Rate × minutes fill.** Anything still missing is the player's per-minute rate times projected minutes.
  - **Rates** come from ESPN season totals. This season counts in full; last season counts as up to 600 minutes of evidence, so it dominates early and fades as games pile up.
  - **Low-minute players** (rookies, returns from injury) with under 300 blended minutes are topped up to 300 with league-average per-minute rates for their position (G/F/C).
  - **Projected minutes** are backed out of the market: the DK-weighted sum of every priced stat divided by the same weighted sum of the player's rates. That number is blended 80/20 with season MPG and capped at 44.
  - **Manual minutes.** Drop a CSV with `Name,Minutes` columns into `minutes/` (date in the filename, for example `minutes/2026-10-27.csv`) to override projected minutes. Overrides change only the rate-filled stats; market-priced stats stay as the market has them.
- **Positional defaults.** Used only when a player has no ESPN stats in either season.
- **Tiers.**
  - Tier A means REB, AST and 3PM all came from markets.
  - Tier B means one of them came from rate × minutes or a default.
  - Tier C means two or more did.
  - `thin` means no points rung between 15% and 85% has a spread of 6¢ or less.
- **Turnovers.** Kalshi has no turnover market, so turnovers are always the turnover rate times projected minutes. With no ESPN stats, they fall back to 0.3 + 0.06 × PTS + 0.17 × AST.
- **DD/TD.** Computed from the five fitted stats assuming independence. Blended 50/50 with the Kalshi DD/TD market when that market's spread is 15¢ or less.
- **Scoring.** DK classic: PTS 1, 3PM 0.5, REB 1.25, AST 1.5, STL 2, BLK 2, TOV -0.5, DD +1.5, TD +3 (stacked with DD).
- **Players covered.** Only players Kalshi lists get a projection. Deep bench players with no points market do not appear.

## Salaries

DraftKings salaries come from DK's public lobby and draftables endpoints, using the classic slate with the most games for each date. Those endpoints are undocumented and could change.

If the DK pull fails, the dashboard still works. Salary and value columns go blank and the error shows at the top of the page.

To override by hand, drop a DK salary export into `salaries/` with the date in the filename, for example `salaries/DKSalaries_2026-10-27.csv`. Manual files take priority over the automatic pull.

## Things to know

- **Inactivity.** GitHub disables scheduled workflows in a public repo after 60 days with no repository activity. The daily archive commit keeps it active through the season. After a long offseason, re-enable the workflow from the Actions tab.
- **Private repos.** About 70 runs a day, roughly 1 minute each, is far past the 2,000 free Actions minutes a private repo gets. Keep the repo public; Kalshi data is public anyway.
- **Custom domain.** With a custom Pages domain, set a repository variable `SITE_URL` to the site's URL so the job can read back its own history.
- **Local run.** `python scripts/pull.py --out site`, then serve `site/` with `index.html` copied in.

## Files

```
index.html                     dashboard (single file, no dependencies)
scripts/pull.py                puller + model (stdlib only)
.github/workflows/update.yml   schedule, archive commit, Pages deploy
archive/                       one JSON per finished slate (committed by the workflow)
salaries/                      optional manual DK salary CSVs
minutes/                       optional manual minutes CSVs (Name,Minutes)
```
