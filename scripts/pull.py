#!/usr/bin/env python3
"""
Kalshi NBA player prop puller.

Pulls every open Kalshi NBA player prop market, measures spread and order book
depth, fits a prop-implied mean for each player stat from the Kalshi ladders,
converts to DraftKings classic scoring, and writes JSON for the static dashboard.

Standard library only. Run locally:
    python scripts/pull.py --out site
GitHub Actions runs it on a schedule (see .github/workflows/update.yml).
"""
import argparse, csv, json, math, os, re, sys, time, unicodedata
import urllib.request, urllib.error, urllib.parse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

API = "https://api.elections.kalshi.com/trade-api/v2"
ET = ZoneInfo("America/New_York")
UA = {"User-Agent": "kalshi-props-dashboard/1.0", "Accept": "application/json"}

# series ticker -> stat key. Duplicate-looking series are both listed because
# Kalshi has run more than one ticker for the same market type; empty ones are skipped.
SERIES = {
    "KXNBAPTS": "pts", "KXNBAREB": "reb", "KXNBAAST": "ast", "KXNBA3PT": "threes",
    "KXNBASTL": "stl", "KXNBABLK": "blk", "KXNBA2D": "dd", "KXNBA3D": "td",
    "KXNBAPRA": "pra", "KXNBAPR": "pr", "KXNBAPA": "pa", "KXNBARA": "ra",
    "KXNBASTOCK": "stk", "KXNBASTOCKS": "stk",
}
SINGLE = ["pts", "reb", "ast", "threes", "stl", "blk"]
COMBO = {"pra": ("pts", "reb", "ast"), "pr": ("pts", "reb"), "pa": ("pts", "ast"),
         "ra": ("reb", "ast"), "stk": ("stl", "blk")}
# variance multiplier: var = K * mean (negative binomial when K > 1)
K = {"pts": 2.2, "reb": 1.3, "ast": 1.3, "threes": 1.25, "stl": 1.1, "blk": 1.3,
     "pra": 2.6, "pr": 2.4, "pa": 2.4, "ra": 1.6, "stk": 1.2}
MAX_SPREAD_FIT = 20      # cents; wider markets are ignored by the fit
DEPTH_BAND = 0.05        # depth is measured within this many dollars of mid
MON = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL",
                                    "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


# ---------------------------------------------------------------- http
def get_json(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429 or e.code >= 500:
                time.sleep(1.5 * (i + 1)); continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"failed after {tries} tries: {url}")


def fixture_or_get(fixture, name, url):
    if fixture:
        p = os.path.join(fixture, name)
        return json.load(open(p)) if os.path.exists(p) else None
    return get_json(url)


# ---------------------------------------------------------------- helpers
def norm(n):
    n = unicodedata.normalize("NFKD", n or "").encode("ascii", "ignore").decode()
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\.?$", "", n.strip(), flags=re.I)
    return re.sub(r"[^a-z]", "", n.lower())


def cents(x):
    try: return int(round(float(x) * 100))
    except (TypeError, ValueError): return None


def num(x):
    try: return float(x)
    except (TypeError, ValueError): return 0.0


def parse_event(event_ticker):
    """KXNBAPTS-26OCT27DETNYK -> ('2026-10-27', 'DET', 'NYK')"""
    seg = event_ticker.split("-")[1]
    m = re.match(r"(\d{2})([A-Z]{3})(\d{2})([A-Z]+)$", seg)
    if not m:
        return None, None, None
    yy, mon, dd, teams = m.groups()
    date = f"20{yy}-{MON[mon]:02d}-{dd}"
    half = len(teams) // 2
    return date, teams[:half], teams[half:]


def team_from_ticker(ticker, away, home):
    tail = ticker.split("-")[2] if ticker.count("-") >= 2 else ""
    for t in (away, home):
        if t and tail.startswith(t):
            return t
    return ""


# ---------------------------------------------------------------- pull
def pull_markets(fixture):
    out, errors = [], []
    seen = set()
    for series, stat in SERIES.items():
        if len(errors) >= 2 and not out:
            errors.append("Kalshi unreachable; skipped remaining series"); break
        cursor = ""
        for page in range(20):
            q = {"series_ticker": series, "status": "open", "limit": 1000}
            if cursor: q["cursor"] = cursor
            url = f"{API}/markets?{urllib.parse.urlencode(q)}"
            try:
                j = fixture_or_get(fixture, f"markets_{series}.json", url) if page == 0 or not fixture else None
            except Exception as e:
                errors.append(f"{series}: {e}"); break
            if not j: break
            for m in j.get("markets", []):
                if m["ticker"] in seen: continue
                seen.add(m["ticker"])
                date, away, home = parse_event(m["event_ticker"])
                sub = m.get("yes_sub_title") or m.get("title") or ""
                player = sub.split(":")[0].strip()
                bid, ask = cents(m.get("yes_bid_dollars")), cents(m.get("yes_ask_dollars"))
                out.append({
                    "ticker": m["ticker"], "series": series, "stat": stat,
                    "date": date, "game": f"{away}@{home}" if away else "",
                    "team": team_from_ticker(m["ticker"], away, home),
                    "player": player, "line": m.get("floor_strike"),
                    "bid": bid, "ask": ask,
                    "bid_sz": num(m.get("yes_bid_size_fp")), "ask_sz": num(m.get("yes_ask_size_fp")),
                    "last": cents(m.get("last_price_dollars")),
                    "vol": num(m.get("volume_fp")), "vol24": num(m.get("volume_24h_fp")),
                    "oi": num(m.get("open_interest_fp")),
                    "close": m.get("close_time"),
                })
            cursor = j.get("cursor") or ""
            if not cursor or fixture: break
    return out, errors


def pull_depth(markets, fixture):
    """Batch orderbooks; adds yes-side and no-side dollar depth within DEPTH_BAND of mid."""
    tick = [m["ticker"] for m in markets]
    books = {}
    if fixture:
        j = fixture_or_get(fixture, "orderbooks.json", None) or {}
        for ob in j.get("orderbooks", []): books[ob["ticker"]] = ob.get("orderbook_fp", {})
    else:
        for i in range(0, len(tick), 100):
            chunk = tick[i:i + 100]
            q = "&".join("tickers=" + urllib.parse.quote(t) for t in chunk)
            try:
                j = get_json(f"{API}/markets/orderbooks?{q}") or {}
            except Exception:
                j = {}
            for ob in j.get("orderbooks", []):
                books[ob["ticker"]] = ob.get("orderbook_fp", {})
            time.sleep(0.15)
    for m in markets:
        ob = books.get(m["ticker"])
        m["depth_bid"] = m["depth_ask"] = None
        if not ob or m["bid"] is None or m["ask"] is None: continue
        mid = (m["bid"] + m["ask"]) / 200
        yes = [(float(p), float(q)) for p, q in ob.get("yes_dollars") or []]
        no = [(float(p), float(q)) for p, q in ob.get("no_dollars") or []]
        # resting YES bids near mid: what a seller of YES can hit
        m["depth_bid"] = round(sum(p * q for p, q in yes if p >= mid - DEPTH_BAND), 2)
        # resting NO bids near mid = YES offers at 1 - p: what a buyer of YES can lift
        m["depth_ask"] = round(sum((1 - p) * q for p, q in no if (1 - p) <= mid + DEPTH_BAND), 2)


# ---------------------------------------------------------------- model
def nb_sf(line, mu, k):
    """P(X > line), X ~ NB with mean mu, var k*mu (Poisson if k<=1)."""
    mu = max(mu, 1e-4)
    n = int(math.floor(line))
    if n < 0: return 1.0
    if k <= 1.0001:
        pmf = math.exp(-mu); cdf = pmf
        for x in range(n):
            pmf *= mu / (x + 1); cdf += pmf
        return max(0.0, 1 - cdf)
    r = mu / (k - 1); p = r / (r + mu)
    pmf = math.exp(r * math.log(p)); cdf = pmf
    for x in range(n):
        pmf *= (x + r) / (x + 1) * (1 - p); cdf += pmf
    return max(0.0, 1 - cdf)


def logit(p):
    p = min(max(p, 0.02), 0.98); return math.log(p / (1 - p))


def fit_mean(obs, k, hi):
    def loss(mu): return sum(w * (logit(nb_sf(L, mu, k)) - logit(p)) ** 2 for L, p, w in obs)
    step = hi / 120
    grid = [step * i for i in range(1, 121)]
    best = min(grid, key=loss)
    fine = [best - step + step * i / 25 for i in range(51)]
    return min((g for g in fine if g > 0), key=loss)


def poisson_binomial_at_least(ps, n):
    dist = [1.0]
    for p in ps:
        nd = [0.0] * (len(dist) + 1)
        for i, v in enumerate(dist):
            nd[i] += v * (1 - p); nd[i + 1] += v * p
        dist = nd
    return sum(dist[n:])


# ---------------------------------------------------------------- season stats (fallback rates)
DKW = {"pts": 1.0, "reb": 1.25, "ast": 1.5, "threes": 0.5, "stl": 2.0, "blk": 2.0}
RATE_STATS = ["pts", "reb", "ast", "threes", "stl", "blk", "tov"]
PRIOR_WEIGHT_MIN = 600   # last season counts as up to this many minutes of evidence
PRIOR_WEIGHT_GP = 20     # same idea for minutes per game
POS_FILL_MIN = 300       # low-evidence players are topped up to this many minutes with positional average rates
IMPLIED_WEIGHT = 0.8     # projected minutes = 80% market-implied, 20% blended season MPG
MAX_MIN = 44


def espn_season(now):
    d = now.astimezone(ET)
    return d.year + 1 if d.month >= 8 else d.year


def pull_espn_season(season, fixture, errors):
    url = ("https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/statistics/byathlete"
           f"?region=us&lang=en&contentorigin=espn&isqualified=false&page=1&limit=1000&season={season}&seasontype=2")
    try:
        j = fixture_or_get(fixture, f"espn_stats_{season}.json", url)
    except Exception as e:
        errors.append(f"ESPN stats {season}: {e}"); return []
    if not j:
        return []
    names = {c["name"]: c.get("names", []) for c in j.get("categories", [])}

    def val(a, cat, field):
        c = next((x for x in a.get("categories", []) if x["name"] == cat), None)
        if not c or field not in names.get(cat, []): return 0.0
        try: return float(c["values"][names[cat].index(field)] or 0)
        except (IndexError, TypeError, ValueError): return 0.0

    out = []
    for a in j.get("athletes", []):
        ath = a.get("athlete", {})
        mins = val(a, "general", "minutes")
        if mins <= 0: continue
        team = ath.get("teamShortName", "")
        pos = ((ath.get("position") or {}).get("abbreviation") or "F")[-1:].upper()
        out.append({"id": str(ath.get("id", "")), "name": ath.get("displayName", ""),
                    "pos": pos if pos in ("G", "F", "C") else "F",
                    "team": ESPN_TO_KALSHI.get(team, team), "gp": val(a, "general", "gamesPlayed"), "min": mins,
                    "tot": {"pts": val(a, "offensive", "points"), "reb": val(a, "general", "rebounds"),
                            "ast": val(a, "offensive", "assists"),
                            "threes": val(a, "offensive", "threePointFieldGoalsMade"),
                            "stl": val(a, "defensive", "steals"), "blk": val(a, "defensive", "blocks"),
                            "tov": val(a, "offensive", "turnovers")}})
    return out


class Rates:
    """Per-minute rates blending this season with last season."""

    def __init__(self, cur, prev, season):
        self.season = season
        self.cur, self.prev_by_id, self.prev_by_name = {}, {}, {}
        for r in cur: self.cur.setdefault(norm(r["name"]), []).append(r)
        for r in prev:
            self.prev_by_id[r["id"]] = r
            self.prev_by_name.setdefault(norm(r["name"]), []).append(r)
        self.n_cur, self.n_prev = len(cur), len(prev)
        # positional per-minute averages (G/F/C) from last season's rotation players (200+ min);
        # if last season failed to load, use this season's players with 30+ min
        base = [r for r in prev if r["min"] >= 200] or [r for r in cur if r["min"] >= 30]
        pool = {}
        for r in base:
            agg = pool.setdefault(r["pos"], {"min": 0.0, **{s: 0.0 for s in RATE_STATS}})
            agg["min"] += r["min"]
            for s in RATE_STATS: agg[s] += r["tot"][s]
        self.pos_rate = {p: {s: a[s] / a["min"] for s in RATE_STATS} for p, a in pool.items() if a["min"] > 0}

    @staticmethod
    def _pick(cands, team):
        if not cands: return None
        if len(cands) == 1: return cands[0]
        hit = [c for c in cands if c["team"] == team]
        return hit[0] if len(hit) == 1 else None

    def get(self, key, team):
        c = self._pick(self.cur.get(key, []), team)
        p = self.prev_by_id.get(c["id"]) if c else None
        if p is None:
            p = self._pick(self.prev_by_name.get(key, []), team)
        if not c and not p: return None
        cm, cg = (c["min"], c["gp"]) if c else (0.0, 0.0)
        w = min(p["min"], PRIOR_WEIGHT_MIN) if p else 0.0
        posr = self.pos_rate.get((c or p)["pos"])
        fill = max(0.0, POS_FILL_MIN - (cm + w)) if posr else 0.0
        rate = {}
        for s in RATE_STATS:
            prev_rate = p["tot"][s] / p["min"] if p else 0.0
            rate[s] = ((c["tot"][s] if c else 0.0) + prev_rate * w + (posr[s] * fill if fill else 0.0)) / (cm + w + fill)
        wg = min(p["gp"], PRIOR_WEIGHT_GP) if p else 0.0
        prev_mpg = p["min"] / p["gp"] if p and p["gp"] else 0.0
        mpg = (cm + prev_mpg * wg) / (cg + wg) if cg + wg else None
        basis = ("cur+prev" if c and p else "cur only" if c else "prev only") + ("+pos" if fill else "")
        return {"rate": rate, "mpg": mpg, "basis": basis,
                "cur_min": cm, "prev_min": p["min"] if p else 0.0}


def load_rates(now, fixture, errors):
    season = espn_season(now)
    cur = pull_espn_season(season, fixture, errors)
    prev = pull_espn_season(season - 1, fixture, errors)
    return Rates(cur, prev, season)


def load_minutes(repo_root):
    """Optional manual minutes: minutes/*.csv with Name,Minutes (date in filename optional)."""
    mins = {}
    d = os.path.join(repo_root, "minutes")
    if not os.path.isdir(d): return mins
    for fn in os.listdir(d):
        if not fn.lower().endswith(".csv"): continue
        m = re.search(r"(\d{4}-\d{2}-\d{2})", fn)
        date = m.group(1) if m else None
        for r in csv.DictReader(open(os.path.join(d, fn), encoding="utf-8-sig")):
            name = r.get("Name") or r.get("name") or r.get("Player") or ""
            val = r.get("Minutes") or r.get("minutes") or r.get("MIN") or ""
            try: mins[(date, norm(name))] = float(val)
            except ValueError: pass
    return mins


def build_projections(markets, salaries, rates=None, manual_min=None):
    manual_min = manual_min or {}
    groups = {}
    for m in markets:
        key = (m["date"], norm(m["player"]))
        g = groups.setdefault(key, {"player": m["player"], "team": m["team"], "game": m["game"],
                                    "date": m["date"], "obs": {}, "ddtd": {}})
        if m["bid"] is None or m["ask"] is None: continue
        spread = m["ask"] - m["bid"]; mid = (m["bid"] + m["ask"]) / 200
        if m["stat"] in ("dd", "td"):
            if spread <= 15: g["ddtd"][m["stat"]] = mid
            continue
        if spread > MAX_SPREAD_FIT or mid < 0.04 or mid > 0.96 or m["line"] is None: continue
        w = 1.5 / (1 + (spread / 4) ** 2)
        g["obs"].setdefault(m["stat"], []).append((float(m["line"]), mid, w, spread))

    rows = []
    for (date, key), g in groups.items():
        if "pts" not in g["obs"]: continue
        sal = salaries.get((date, key)) or salaries.get((None, key))
        pos = sal["pos"] if sal else ""
        big = "C" in pos
        mu, flags, src, ssrc = {}, [], {}, {}
        for s in list(SINGLE) + list(COMBO):
            o = g["obs"].get(s)
            if o:
                mu[s] = fit_mean([(L, p, w) for L, p, w, _ in o], K[s], 70 if s in ("pts", "pra", "pr", "pa") else 20)
                src[s] = len(o)
                if s in SINGLE: ssrc[s] = "mkt"
        # 1) fill missing singles from combo markets
        for s in ("reb", "ast", "stl", "blk"):
            if s in mu: continue
            for c, parts in COMBO.items():
                if c in mu and s in parts and all(x in mu for x in parts if x != s):
                    v = mu[c] - sum(mu[x] for x in parts if x != s)
                    if v > 0:
                        mu[s] = v; ssrc[s] = "combo"; flags.append(f"{s} from {c}"); break
        # 2) projected minutes: market-implied from the priced stats, shrunk toward season MPG
        r = rates.get(key, g["team"]) if rates else None
        mins, min_src = None, None
        if (date, key) in manual_min or (None, key) in manual_min:
            mins = manual_min.get((date, key), manual_min.get((None, key))); min_src = "manual"
        elif r:
            priced = [s for s in SINGLE if s in ssrc]
            den = sum(DKW[s] * r["rate"][s] for s in priced)
            implied = sum(DKW[s] * mu[s] for s in priced) / den if den > 0 else None
            if implied is not None:
                mins = IMPLIED_WEIGHT * implied + (1 - IMPLIED_WEIGHT) * (r["mpg"] or implied)
                min_src = "implied"
        if mins is not None:
            mins = max(0.0, min(MAX_MIN, mins))
        # 3) still missing: per-minute rate x projected minutes, else positional default
        core_def = 0
        dflt = {"reb": 6.0 if big else 3.5 if pos else 4.5, "ast": 1.6 if big else 1.8,
                "threes": 0.3 if big else 0.9 if pos else 0.6,
                "stl": 0.7, "blk": 0.9 if big else 0.5 if "PF" in pos else 0.3 if pos else 0.5}
        for s in SINGLE:
            if s in mu: continue
            if r and mins is not None:
                mu[s] = max(0.01, r["rate"][s] * mins); ssrc[s] = "rate"
                flags.append(f"{s} rate x min ({r['basis']})")
            else:
                mu[s] = dflt[s]; ssrc[s] = "default"; flags.append(f"{s} default")
            if s in ("reb", "ast", "threes"): core_def += 1
        if r and mins is not None:
            tov = r["rate"]["tov"] * mins; ssrc["tov"] = "rate"
        else:
            tov = 0.3 + 0.06 * mu["pts"] + 0.17 * mu["ast"]; ssrc["tov"] = "formula"
        ps10 = [nb_sf(9.5, mu[s], K[s]) for s in ("pts", "reb", "ast", "stl", "blk")]
        pdd, ptd = poisson_binomial_at_least(ps10, 2), poisson_binomial_at_least(ps10, 3)
        if "dd" in g["ddtd"]: pdd = 0.5 * pdd + 0.5 * g["ddtd"]["dd"]; flags.append("dd blended")
        if "td" in g["ddtd"]: ptd = 0.5 * ptd + 0.5 * g["ddtd"]["td"]; flags.append("td blended")
        dk = (mu["pts"] + 0.5 * mu["threes"] + 1.25 * mu["reb"] + 1.5 * mu["ast"] + 2 * mu["stl"]
              + 2 * mu["blk"] - 0.5 * tov + 1.5 * pdd + 3 * ptd)
        pts_obs = g["obs"]["pts"]
        thin = not any(0.15 <= p <= 0.85 and sp <= 6 for _, p, _, sp in pts_obs)
        rows.append({
            "date": date, "player": g["player"], "team": g["team"], "game": g["game"],
            "pos": pos, "salary": sal["salary"] if sal else None, "dk_id": sal["id"] if sal else None,
            "pts": round(mu["pts"], 2), "reb": round(mu["reb"], 2), "ast": round(mu["ast"], 2),
            "threes": round(mu["threes"], 2), "stl": round(mu["stl"], 2), "blk": round(mu["blk"], 2),
            "tov": round(tov, 2), "p_dd": round(pdd, 3), "p_td": round(ptd, 3),
            "dk": round(dk, 2), "value": round(dk / sal["salary"] * 1000, 2) if sal and sal["salary"] else None,
            "tier": "A" if core_def == 0 else "B" if core_def == 1 else "C",
            "min": round(mins, 1) if mins is not None else None, "min_src": min_src,
            "mpg": round(r["mpg"], 1) if r and r["mpg"] else None, "src": ssrc,
            "thin": thin, "n_mkts": sum(src.values()),
            "pts_spread": min([sp for _, p, _, sp in pts_obs if 0.15 <= p <= 0.85] or [sp for *_, sp in pts_obs]),
            "flags": flags,
        })
    rows.sort(key=lambda r: (r["date"], -r["dk"]))
    return rows


# ---------------------------------------------------------------- schedule (tip times)
ESPN_TO_KALSHI = {"NY": "NYK", "SA": "SAS", "GS": "GSW", "NO": "NOP", "UTAH": "UTA", "WSH": "WAS"}


def load_schedule(dates, fixture, errors):
    """{ 'AWY@HOM': {'tip': iso, 'state': 'pre'|'in'|'post'} } from ESPN's public scoreboard."""
    sched = {}
    for d in dates:
        url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={d.replace('-', '')}"
        try:
            j = fixture_or_get(fixture, f"espn_{d}.json", url) or {}
        except Exception as e:
            errors.append(f"ESPN schedule {d}: {e}"); continue
        for ev in j.get("events", []):
            comp = ev["competitions"][0]
            side = {c["homeAway"]: ESPN_TO_KALSHI.get(c["team"]["abbreviation"], c["team"]["abbreviation"])
                    for c in comp["competitors"]}
            sched[f"{side.get('away')}@{side.get('home')}"] = {
                "tip": ev.get("date"), "state": ev.get("status", {}).get("type", {}).get("state", "pre"), "date": d}
    return sched


def game_started(game, sched, now):
    g = sched.get(game)
    if not g:
        # tolerate abbreviation mismatches: match on either team code
        a, h = (game.split("@") + [""])[:2]
        hits = [v for k, v in sched.items() if k.split("@")[0] == a or k.split("@")[1] == h]
        g = hits[0] if len(hits) == 1 else None
    if not g:
        return False, None
    tip = datetime.fromisoformat(g["tip"].replace("Z", "+00:00")) if g.get("tip") else None
    started = g["state"] != "pre" or (tip is not None and now >= tip)
    return started, g.get("tip")


# ---------------------------------------------------------------- DK salaries
def load_salaries(dates, repo_root, fixture, errors):
    """Manual override first (salaries/*.csv in the repo), then DraftKings public endpoints."""
    sal = {}
    d = os.path.join(repo_root, "salaries")
    if os.path.isdir(d):
        for fn in os.listdir(d):
            if not fn.lower().endswith(".csv"): continue
            m = re.search(r"(\d{4}-\d{2}-\d{2})", fn)
            date = m.group(1) if m else None
            for r in csv.DictReader(open(os.path.join(d, fn), encoding="utf-8-sig")):
                sal[(date, norm(r["Name"]))] = {"salary": int(r["Salary"]), "pos": r["Position"], "id": r["ID"]}
    if fixture:
        return sal, f"manual CSV only ({len(sal)} players)"
    try:
        lobby = get_json("https://www.draftkings.com/lobby/getcontests?sport=NBA") or {}
        groups = lobby.get("DraftGroups") or lobby.get("draftGroups") or []
        status = []
        for date in dates:
            cands = []
            for g in groups:
                start = str(g.get("StartDateEst") or g.get("startDateEst") or "")
                if not start.startswith(date): continue
                gtype = str(g.get("GameType") or g.get("gameType") or "")
                suffix = g.get("ContestStartTimeSuffix") or ""
                if gtype and "classic" not in gtype.lower(): continue
                if "showdown" in str(suffix).lower() or "tiers" in str(suffix).lower(): continue
                cands.append(g)
            if not cands:
                status.append(f"{date}: no DK classic slate found"); continue
            g = max(cands, key=lambda g: (g.get("GameCount") or 0, g.get("DraftGroupTag") == "Featured"))
            gid = g.get("DraftGroupId") or g.get("draftGroupId")
            dj = get_json(f"https://api.draftkings.com/draftgroups/v1/draftgroups/{gid}/draftables") or {}
            n = 0
            for x in dj.get("draftables", []):
                k = (date, norm(x.get("displayName") or f"{x.get('firstName','')} {x.get('lastName','')}"))
                if k in sal: continue
                sal[k] = {"salary": x.get("salary"), "pos": x.get("position", ""), "id": x.get("draftableId")}
                n += 1
            status.append(f"{date}: DK group {gid}, {n} players")
        return sal, "; ".join(status)
    except Exception as e:
        errors.append(f"DK salaries: {e}")
        return sal, "DK pull failed (manual salaries/ CSVs still used)"


# ---------------------------------------------------------------- history
def load_history(date, out_dir, site_url):
    p = os.path.join(out_dir, "data", "history", f"{date}.json")
    if os.path.exists(p):
        return json.load(open(p))
    if site_url:
        try:
            h = get_json(f"{site_url.rstrip('/')}/data/history/{date}.json", tries=2)
            if h: return h
        except Exception:
            pass
    return {"date": date, "t": [], "m": {}, "meta": {}, "p": {}, "proj_last": []}


def update_history(h, ts, markets, proj):
    """markets/proj passed here are pregame only; in-game prices never enter history."""
    if not markets and not proj:
        return h
    h["t"].append(ts); ti = len(h["t"]) - 1
    for m in markets:
        cur = [m["bid"], m["ask"], round(m["vol"])]
        series = h["m"].setdefault(m["ticker"], [])
        if not series or series[-1][1:] != cur:
            series.append([ti] + cur)
        h["meta"][m["ticker"]] = {k: m[k] for k in ("player", "team", "game", "stat", "line")}
    for r in proj:
        key = norm(r["player"])
        series = h["p"].setdefault(key, [])
        if not series or series[-1][1] != r["dk"]:
            series.append([ti, r["dk"]])
    if proj:
        fresh = {norm(r["player"]) for r in proj}
        h["proj_last"] = [r for r in h.get("proj_last", []) if norm(r["player"]) not in fresh] + proj
    return h


def build_archive(h):
    close = []
    for t, series in h["m"].items():
        meta = h["meta"].get(t, {})
        first, last = series[0], series[-1]
        mid = lambda s: (s[1] + s[2]) / 2 if s[1] is not None and s[2] is not None else None
        close.append({"ticker": t, **meta, "open_mid": mid(first), "close_mid": mid(last),
                      "close_bid": last[1], "close_ask": last[2], "volume": last[3],
                      "closed_at": h["t"][last[0]]})
    return {"date": h["date"], "snapshots": len(h["t"]), "first": h["t"][0] if h["t"] else None,
            "last": h["t"][-1] if h["t"] else None, "markets": close, "projections": h.get("proj_last", [])}


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site")
    ap.add_argument("--archive", default="archive")
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--fixture", default="")
    ap.add_argument("--site-url", default=os.environ.get("SITE_URL", ""))
    ap.add_argument("--now", default="", help="testing only: ISO UTC timestamp to treat as now")
    a = ap.parse_args()

    if not a.site_url and os.environ.get("GITHUB_REPOSITORY"):
        owner, repo = os.environ["GITHUB_REPOSITORY"].split("/")
        a.site_url = f"https://{owner.lower()}.github.io/{repo}"

    now = (datetime.fromisoformat(a.now.replace("Z", "+00:00")) if a.now else datetime.now(timezone.utc))
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    errors = []
    markets, e = pull_markets(a.fixture); errors += e
    if not markets and errors:
        # Kalshi down or blocked: exit non-zero so the workflow skips deploy and the last good site stays up
        print(f"{ts}: Kalshi pull failed, nothing written"); [print("  !", x) for x in errors]
        sys.exit(1)
    pull_depth(markets, a.fixture)
    dates = sorted({m["date"] for m in markets if m["date"]})
    sched = load_schedule(dates, a.fixture, errors)
    for m in markets:
        m["started"], m["tip"] = game_started(m["game"], sched, now)
    games = {}
    for m in markets:
        g = sched.get(m["game"], {})
        games.setdefault(m["game"], {"game": m["game"], "date": m["date"], "tip": m["tip"],
                                     "state": g.get("state", "pre" if not m["started"] else "in"),
                                     "started": m["started"]})
    salaries, sal_status = load_salaries(dates, a.repo_root, a.fixture, errors)
    rates = load_rates(now, a.fixture, errors) if markets else None
    manual_min = load_minutes(a.repo_root)
    pre = [m for m in markets if not m["started"]]
    proj_pre = build_projections(pre, salaries, rates, manual_min)

    os.makedirs(os.path.join(a.out, "data", "history"), exist_ok=True)
    os.makedirs(a.archive, exist_ok=True)

    # history per slate date; carry forward dates still in progress
    hist_dates = set(dates)
    idx_path = os.path.join(a.out, "data", "index.json")
    prev_idx = {}
    if os.path.exists(idx_path):
        prev_idx = json.load(open(idx_path))
    elif a.site_url:
        try: prev_idx = get_json(f"{a.site_url.rstrip('/')}/data/index.json", tries=2) or {}
        except Exception: prev_idx = {}
    hist_dates |= set(prev_idx.get("live_dates", []))

    today_et = (now.astimezone(ET) - timedelta(hours=6)).date().isoformat()  # slate day rolls at 6am ET
    archived = []
    live = []
    proj = []
    for d in sorted(hist_dates):
        h = load_history(d, a.out, a.site_url)
        dm_pre = [m for m in pre if m["date"] == d]
        h = update_history(h, ts, dm_pre, [r for r in proj_pre if r["date"] == d])
        # projections for this date: live for pregame games, locked pregame close for started games
        started_games = {g for g, v in games.items() if v["date"] == d and v["started"]}
        live_rows = [r for r in proj_pre if r["date"] == d]
        locked = [dict(r, locked=True) for r in h.get("proj_last", []) if r["game"] in started_games]
        proj += live_rows + locked
        json.dump(h, open(os.path.join(a.out, "data", "history", f"{d}.json"), "w"), separators=(",", ":"))
        ap_path = os.path.join(a.archive, f"{d}.json")
        has_open = any(m["date"] == d for m in markets)
        # a slate is final once its day has rolled over (6am ET); in-game and settled prices are ignored
        if d < today_et and h["t"]:
            if not os.path.exists(ap_path):
                json.dump(build_archive(h), open(ap_path, "w"), separators=(",", ":"))
                archived.append(d)
        elif d >= today_et:
            live.append(d)
    proj = sorted((r for r in proj if r["date"] >= today_et), key=lambda r: (r["date"], -r["dk"]))
    markets = [m for m in markets if m["date"] >= today_et]   # drop stale prior-day markets from the live view
    dates = [d for d in dates if d >= today_et]

    archive_dates = sorted(f[:-5] for f in os.listdir(a.archive) if re.match(r"\d{4}-\d{2}-\d{2}\.json$", f))
    latest = {"generated_at": ts, "dates": dates, "markets": markets, "projections": proj,
              "games": sorted(games.values(), key=lambda g: (g["date"], g["tip"] or "")),
              "salaries": sal_status, "errors": errors,
              "stats": (f"ESPN {rates.season - 1}-{str(rates.season)[2:]}: {rates.n_cur} players, "
                        f"{rates.season - 2}-{str(rates.season - 1)[2:]}: {rates.n_prev} players"
                        + (f"; manual minutes for {len(manual_min)}" if manual_min else "")) if rates else "",
              "settings": {"max_spread_fit": MAX_SPREAD_FIT, "depth_band_c": int(DEPTH_BAND * 100)}}
    json.dump(latest, open(os.path.join(a.out, "data", "latest.json"), "w"), separators=(",", ":"))
    json.dump({"generated_at": ts, "live_dates": live, "archive_dates": archive_dates},
              open(idx_path, "w"), separators=(",", ":"))

    print(f"{ts}: {len(markets)} markets, {len(proj)} projections, dates {dates}, "
          f"archived {archived}, salaries: {sal_status}, errors: {len(errors)}")
    for x in errors: print("  !", x)
    if not markets and not errors:
        print("  no open markets (off day)")


if __name__ == "__main__":
    main()
