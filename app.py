#!/usr/bin/env python3
"""
TwinPicks HR Finder — Standalone
=================================
Dedicated HR research tool. Filters to players with sportsbook HR props,
uses starting pitcher as the matchup lens, full Statcast profile.

Data sources:
  - MLB Stats API (free): today's games, probable pitchers, lineups, pitcher stats
  - Baseball Savant (free): EV, barrel%, LA, pull%, FB%, xwOBA, ISO, BIP
  - The Odds API: sportsbook HR props (FanDuel)
"""

import os, json, csv, io, threading
import urllib.request, urllib.error
from datetime import datetime
from flask import Flask, jsonify, render_template_string

try:
    import pytz
    EST = pytz.timezone("US/Eastern")
except Exception:
    EST = None

app = Flask(__name__)

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
DATA_DIR = "/data" if os.path.isdir("/data") else "."

# ── Helpers ──────────────────────────────────────────────────────────────────

def now_est():
    return datetime.now(EST) if EST else datetime.now()

def today_str():
    return now_est().strftime("%Y-%m-%d")

def _f(v):
    try:
        f = float(v) if v not in (None, "", "null") else 0.0
        return 0.0 if f != f else f
    except Exception:
        return 0.0

def _i(v):
    try:
        return int(_f(v))
    except Exception:
        return 0

# ── Park Factors ─────────────────────────────────────────────────────────────

PARK_FACTORS = {
    "COL": 1.15, "CIN": 1.06, "PHI": 1.05, "TEX": 1.04, "BAL": 1.04,
    "BOS": 1.03, "NYY": 1.03, "HOU": 1.02, "ATL": 1.02, "CHC": 1.02,
    "MIL": 1.01, "TOR": 1.01, "ARI": 1.00, "LAD": 1.00, "DET": 0.99,
    "WSH": 0.99, "MIN": 0.99, "CWS": 0.99, "STL": 0.98, "PIT": 0.98,
    "NYM": 0.98, "TB":  0.97, "CLE": 0.97, "KC":  0.97, "MIA": 0.96,
    "LAA": 0.96, "SEA": 0.95, "SD":  0.95, "SF":  0.92, "OAK": 0.97,
}

# ── Cache ─────────────────────────────────────────────────────────────────────

_cache = {
    "date": None,
    "candidates": [],
    "games": 0,
    "with_props": 0,
    "last_refresh": None,
    "status": "idle",
    "error": None,
}
_lock = threading.Lock()

# ── MLB Stats API ─────────────────────────────────────────────────────────────

MLB = "https://statsapi.mlb.com/api/v1"

def _mlb_get(url):
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "TwinPicks-HRFinder/1.0")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[MLB] {url[:80]} → {e}")
        return {}

def fetch_games(date=None):
    date = date or today_str()
    d = _mlb_get(f"{MLB}/schedule?sportId=1&date={date}&hydrate=probablePitcher,lineups,team")
    games = []
    for entry in d.get("dates", []):
        for g in entry.get("games", []):
            teams = g.get("teams", {})
            home = teams.get("home", {})
            away = teams.get("away", {})
            ht = home.get("team", {})
            at = away.get("team", {})
            ha, aa = ht.get("abbreviation", ""), at.get("abbreviation", "")
            hsp = home.get("probablePitcher", {})
            asp = away.get("probablePitcher", {})
            lineups = g.get("lineups", {})
            games.append({
                "label":        f"{aa} @ {ha}",
                "home_abbr":    ha,
                "away_abbr":    aa,
                "home_team":    ht.get("name", ""),
                "away_team":    at.get("name", ""),
                "park_factor":  PARK_FACTORS.get(ha, 1.0),
                "home_sp":      {"id": hsp.get("id"), "name": hsp.get("fullName", "")},
                "away_sp":      {"id": asp.get("id"), "name": asp.get("fullName", "")},
                "home_lineup":  [p.get("fullName","") for p in lineups.get("homePlayers", [])],
                "away_lineup":  [p.get("fullName","") for p in lineups.get("awayPlayers", [])],
            })
    print(f"[MLB] {len(games)} games for {date}")
    return games

def fetch_pitcher_stats(pid):
    if not pid:
        return {}
    year = now_est().year
    d = _mlb_get(f"{MLB}/people/{pid}/stats?stats=season&group=pitching&season={year}&sportId=1")
    for split in (d.get("stats") or [{}])[0].get("splits", []):
        s = split.get("stat", {})
        ip_str = str(s.get("inningsPitched", "0"))
        try:
            parts = ip_str.split(".")
            ip = int(parts[0]) + (int(parts[1]) / 3 if len(parts) > 1 else 0)
        except Exception:
            ip = _f(ip_str)
        hr = _i(s.get("homeRuns", 0))
        return {
            "era":        _f(s.get("era", 0)),
            "whip":       _f(s.get("whip", 0)),
            "k9":         _f(s.get("strikeoutsPer9Inn", 0)),
            "hr_allowed": hr,
            "hr_per_9":   round(hr / max(ip, 1) * 9, 2) if ip > 0 else 0,
            "ip":         round(ip, 1),
            "gs":         _i(s.get("gamesStarted", 0)),
        }
    return {}

def fetch_batter_recent(bid):
    if not bid:
        return {}
    year = now_est().year
    d = _mlb_get(f"{MLB}/people/{bid}/stats?stats=season&group=hitting&season={year}&sportId=1")
    for split in (d.get("stats") or [{}])[0].get("splits", []):
        s = split.get("stat", {})
        g = _i(s.get("gamesPlayed", 0))
        hr = _i(s.get("homeRuns", 0))
        return {
            "games":    g,
            "hr":       hr,
            "hr_per_g": round(hr / max(g, 1), 3),
            "avg":      _f(s.get("avg", 0)),
            "slg":      _f(s.get("slg", 0)),
            "ops":      _f(s.get("ops", 0)),
            "ab":       _i(s.get("atBats", 0)),
        }
    return {}

# ── Baseball Savant ───────────────────────────────────────────────────────────

def _sv_col(fn, *candidates):
    lower = {f.strip().lower(): f for f in fn}
    for c in candidates:
        if c in fn:
            return c
        if c.lower() in lower:
            return lower[c.lower()]
    return None

def _sv_get(row, col, default="0"):
    return row.get(col, default) if col else default

def _sv_name(row, nc, fc, lc):
    if nc:
        raw = (row.get(nc) or "").strip()
        if "," in raw:
            parts = raw.split(",", 1)
            last  = parts[0].strip().strip('"')
            first = parts[1].strip().strip('"') if len(parts) > 1 else ""
            return f"{first} {last}".strip() if first else last
        return raw
    fn = (row.get(fc) or "").strip() if fc else ""
    ln = (row.get(lc) or "").strip() if lc else ""
    return f"{fn} {ln}".strip()

def _sv_csv(url, label):
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "TwinPicks-HRFinder/1.0")
        req.add_header("Accept", "text/csv")
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8-sig")
    except Exception as e:
        print(f"[{label}] fetch error: {e}")
        return None, None
    if not raw or len(raw) < 100 or "<html" in raw[:50].lower():
        return None, None
    try:
        reader = csv.DictReader(io.StringIO(raw))
        fn = reader.fieldnames or []
        return fn, list(reader)
    except Exception as e:
        print(f"[{label}] parse error: {e}")
        return None, None

def fetch_savant_ev(ptype="batter", year=None, min_bbe=50):
    year = year or now_est().year
    url = (f"https://baseballsavant.mlb.com/leaderboard/statcast"
           f"?type={ptype}&year={year}&position=&team=&min={min_bbe}&csv=true")
    fn, rows = _sv_csv(url, f"SV-EV-{ptype}")
    if not fn or not rows:
        return fetch_savant_ev(ptype, year - 1, min_bbe) if year == now_est().year else {}

    nc  = _sv_col(fn, "last_name, first_name", "player_name", "name")
    fc  = _sv_col(fn, "first_name", "name_first")
    lc  = _sv_col(fn, "last_name",  "name_last")
    cols = {
        "ev":     _sv_col(fn, "avg_hit_speed",        "avg_exit_velocity"),
        "la":     _sv_col(fn, "avg_hit_angle",         "avg_launch_angle"),
        "ss":     _sv_col(fn, "anglesweetspotpercent", "sweet_spot_percent", "la_sweetspot_percent"),
        "hh":     _sv_col(fn, "ev95percent",           "hard_hit_percent", "hard_hit_pct"),
        "brl":    _sv_col(fn, "brl_percent",           "barrel_batted_rate", "barrel_pct", "brl_pct"),
        "ev50":   _sv_col(fn, "ev50"),
        "max_ev": _sv_col(fn, "max_hit_speed",         "max_exit_velocity"),
        "pull":   _sv_col(fn, "pull_percent",          "pull_pct", "pulled_percent"),
        "oppo":   _sv_col(fn, "opposite_percent",      "oppo_pct", "opposite_field_percent"),
        "fb":     _sv_col(fn, "flyballs_percent",      "fly_ball_percent", "fb_pct"),
        "gb":     _sv_col(fn, "groundballs_percent",   "ground_ball_percent", "gb_pct"),
        "ld":     _sv_col(fn, "linedrives_percent",    "line_drive_percent", "ld_pct"),
        "bbe":    _sv_col(fn, "attempts",              "bbe"),
    }
    print(f"[SV-EV-{ptype}] pull={cols['pull']} fb={cols['fb']} brl={cols['brl']}")
    out = {}
    for r in rows:
        try:
            name = _sv_name(r, nc, fc, lc)
            if not name or len(name) < 3:
                continue
            out[name] = {k: _f(_sv_get(r, cols[k])) for k in cols if cols[k]}
        except Exception:
            continue
    print(f"[SV-EV-{ptype}] {len(out)} players")
    return out

def fetch_savant_xstats(ptype="batter", year=None, min_pa=50):
    year = year or now_est().year
    url = (f"https://baseballsavant.mlb.com/leaderboard/expected_statistics"
           f"?type={ptype}&year={year}&position=&team=&min={min_pa}&csv=true")
    fn, rows = _sv_csv(url, f"SV-xST-{ptype}")
    if not fn or not rows:
        return fetch_savant_xstats(ptype, year - 1, min_pa) if year == now_est().year else {}

    nc = _sv_col(fn, "last_name, first_name", "player_name", "name")
    fc = _sv_col(fn, "first_name", "name_first")
    lc = _sv_col(fn, "last_name",  "name_last")
    cols = {
        "pa":    _sv_col(fn, "pa", "plate_appearances"),
        "bip":   _sv_col(fn, "bip", "balls_in_play"),
        "ba":    _sv_col(fn, "ba", "batting_avg", "avg"),
        "xba":   _sv_col(fn, "est_ba", "xba", "expected_ba"),
        "slg":   _sv_col(fn, "slg", "slugging"),
        "xslg":  _sv_col(fn, "est_slg", "xslg", "expected_slg"),
        "woba":  _sv_col(fn, "woba"),
        "xwoba": _sv_col(fn, "est_woba", "xwoba", "expected_woba"),
        "era":   _sv_col(fn, "era"),
        "xera":  _sv_col(fn, "xera"),
    }
    out = {}
    for r in rows:
        try:
            name = _sv_name(r, nc, fc, lc)
            if not name or len(name) < 3:
                continue
            ba  = _f(_sv_get(r, cols["ba"]))
            slg = _f(_sv_get(r, cols["slg"]))
            row = {k: _f(_sv_get(r, cols[k])) for k in cols if cols[k]}
            row["iso"] = max(0.0, round(slg - ba, 3)) if slg > 0 and ba > 0 else 0.0
            out[name] = row
        except Exception:
            continue
    print(f"[SV-xST-{ptype}] {len(out)} players")
    return out

# ── Odds API ──────────────────────────────────────────────────────────────────

def fetch_hr_props():
    if not ODDS_API_KEY:
        return {}
    try:
        req = urllib.request.Request(
            f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events"
            f"?apiKey={ODDS_API_KEY}&dateFormat=iso")
        req.add_header("User-Agent", "TwinPicks-HRFinder/1.0")
        with urllib.request.urlopen(req, timeout=15) as r:
            events = json.loads(r.read().decode())
    except Exception as e:
        print(f"[Odds] events error: {e}")
        return {}

    today = today_str()
    events = [e for e in events if e.get("commence_time","").startswith(today)]
    props = {}
    for event in events[:20]:
        eid = event.get("id")
        if not eid:
            continue
        try:
            req = urllib.request.Request(
                f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{eid}/odds"
                f"?apiKey={ODDS_API_KEY}&markets=batter_home_runs"
                f"&bookmakers=fanduel&oddsFormat=american")
            req.add_header("User-Agent", "TwinPicks-HRFinder/1.0")
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode())
            glabel = event.get("away_team","") + " @ " + event.get("home_team","")
            for bk in data.get("bookmakers", []):
                for mkt in bk.get("markets", []):
                    if mkt.get("key") != "batter_home_runs":
                        continue
                    for outcome in mkt.get("outcomes", []):
                        pname = outcome.get("description") or outcome.get("name","")
                        side  = outcome.get("name","").lower()
                        line  = _f(outcome.get("point", 0.5))
                        odds  = _i(outcome.get("price", -110))
                        if not pname:
                            continue
                        if pname not in props:
                            props[pname] = {"line": line, "over_odds": None,
                                            "under_odds": None, "game": glabel}
                        if side == "over":
                            props[pname]["over_odds"] = odds
                        elif side == "under":
                            props[pname]["under_odds"] = odds
        except Exception as e:
            print(f"[Odds] event {eid}: {e}")
    print(f"[Odds] {len(props)} HR props")
    return props

# ── Fuzzy name match ──────────────────────────────────────────────────────────

def _match(name, lookup):
    if not name or not lookup:
        return None
    if name in lookup:
        return lookup[name]
    nl = name.lower()
    for k, v in lookup.items():
        if k.lower() == nl:
            return v
    parts = name.split()
    if len(parts) >= 2:
        last, first = parts[-1].lower(), parts[0].lower()
        for k, v in lookup.items():
            kp = k.split()
            if len(kp) >= 2 and kp[-1].lower() == last and kp[0].lower().startswith(first[0]):
                return v
    return None

# ── Scoring ───────────────────────────────────────────────────────────────────

def american_to_implied(odds):
    try:
        o = int(odds)
        return abs(o)/(abs(o)+100) if o <= -100 else 100/(o+100)
    except Exception:
        return 0.5

def score_candidate(bsv, bxst, psv, pxst, pmlb, pf):
    """Return (hr_score, signals[])"""
    sc = 0
    sigs = []

    ev      = bsv.get("ev", 0)
    brl     = bsv.get("brl", 0)
    la      = bsv.get("la", 0)
    ss      = bsv.get("ss", 0)
    pull    = bsv.get("pull", 0)
    fb      = bsv.get("fb", 0)
    hh      = bsv.get("hh", 0)
    iso     = bxst.get("iso", 0)
    xwoba   = bxst.get("xwoba", 0)
    xslg    = bxst.get("xslg", 0)
    slg     = bxst.get("slg", 0)

    # 1. Power core (EV + barrel)
    if brl >= 12 and ev >= 91:
        sc += 4; sigs.append(f"Elite power: {ev:.1f} EV, {brl:.1f}% barrel")
    elif brl >= 9 and ev >= 89:
        sc += 3; sigs.append(f"Strong power: {ev:.1f} EV, {brl:.1f}% barrel")
    elif brl >= 7 and ev >= 87:
        sc += 2; sigs.append(f"Good power: {ev:.1f} EV, {brl:.1f}% barrel")
    elif ev > 0 and ev < 86:
        sc -= 3; sigs.append(f"Low EV ({ev:.1f}) — HR unlikely")
    elif ev > 0 and ev < 88:
        sc -= 1

    # 2. Launch angle
    if 18 <= la <= 28:
        sc += 2; sigs.append(f"HR-zone LA: {la:.0f}°")
    elif 14 <= la <= 32:
        sc += 1
    elif la > 0 and (la > 36 or la < 8):
        sc -= 1

    # 3. ISO
    if iso >= 0.250:
        sc += 3; sigs.append(f"Elite ISO: .{int(iso*1000):03d}")
    elif iso >= 0.200:
        sc += 2; sigs.append(f"Power ISO: .{int(iso*1000):03d}")
    elif iso >= 0.170:
        sc += 1
    elif 0 < iso < 0.120:
        sc -= 1

    # 4. Pull flyball profile
    if pull >= 48 and fb >= 42:
        sc += 2; sigs.append(f"Pull flyball hitter: {pull:.0f}% pull, {fb:.0f}% FB")
    elif pull >= 44 or fb >= 40:
        sc += 1

    # 5. xwOBA quality
    if xwoba >= 0.400:
        sc += 2; sigs.append(f"Elite xwOBA: .{int(xwoba*1000):03d}")
    elif xwoba >= 0.360:
        sc += 1
    elif 0 < xwoba < 0.280:
        sc -= 1

    # 6. Park
    if pf >= 1.05:
        sc += 3; sigs.append(f"Hitter-friendly park (PF {pf:.2f})")
    elif pf >= 1.02:
        sc += 2
    elif pf >= 1.0:
        sc += 1
    elif pf <= 0.95:
        sc -= 2; sigs.append(f"Pitcher park (PF {pf:.2f})")
    elif pf < 1.0:
        sc -= 1

    # 7. Pitcher HR vulnerability
    hr9   = pmlb.get("hr_per_9", 0)
    pbrl  = psv.get("brl", 0)
    pev   = psv.get("ev", 0)
    pxera = pxst.get("xera", 0)
    pera  = pxst.get("era", 0) or pmlb.get("era", 0)

    if hr9 >= 1.8:
        sc += 3; sigs.append(f"HR-prone pitcher: {hr9:.1f} HR/9")
    elif hr9 >= 1.3:
        sc += 2; sigs.append(f"Pitcher allows {hr9:.1f} HR/9")
    elif hr9 >= 1.0:
        sc += 1
    elif 0 < hr9 <= 0.6:
        sc -= 2; sigs.append(f"HR suppressor: {hr9:.1f} HR/9")

    if pbrl >= 10:
        sc += 2; sigs.append(f"Pitcher allows {pbrl:.1f}% barrels")
    elif pbrl >= 7:
        sc += 1

    if pev >= 90:
        sc += 1

    if 0 < pera < pxera - 0.5:
        sc += 1; sigs.append(f"Pitcher ERA/xERA gap: {pera:.2f} ERA vs {pxera:.2f} xERA")

    # 8. Sweet spot
    if ss >= 38:
        sc += 1

    # 9. xSLG regression
    if xslg > slg + 0.05 > 0:
        sc += 1; sigs.append(f"Power regression due: xSLG .{int(xslg*1000):03d} > SLG .{int(slg*1000):03d}")

    return sc, sigs

def zone_fit(bsv, bxst):
    z = 0
    la, ss   = bsv.get("la",0), bsv.get("ss",0)
    pull, fb  = bsv.get("pull",0), bsv.get("fb",0)
    xwoba    = bxst.get("xwoba",0)
    if 18 <= la <= 28: z += 2
    elif 14 <= la <= 32: z += 1
    if ss >= 38: z += 2
    elif ss >= 30: z += 1
    if pull >= 48: z += 2
    elif pull >= 42: z += 1
    if fb >= 44: z += 1
    if xwoba >= 0.380: z += 1
    return "A" if z >= 7 else "B+" if z >= 5 else "B" if z >= 3 else "C" if z >= 2 else "D"

# ── Build candidates ──────────────────────────────────────────────────────────

def build_candidates(games, hr_props, sv_bat, sv_pit, xst_bat, xst_pit):
    player_map = {}  # name -> {game, team, opp_sp, park_factor}
    for g in games:
        pf = g["park_factor"]
        hsp, asp = g["home_sp"], g["away_sp"]
        for name in g["away_lineup"]:
            player_map[name] = {"game": g["label"], "team": g["away_team"],
                                "abbr": g["away_abbr"], "opp_sp": hsp, "pf": pf}
        for name in g["home_lineup"]:
            player_map[name] = {"game": g["label"], "team": g["home_team"],
                                "abbr": g["home_abbr"], "opp_sp": asp, "pf": pf}

    # Prop players not yet in lineup — guess from game label
    for pname, pdata in hr_props.items():
        if _match(pname, player_map):
            continue
        glabel = pdata.get("game","")
        for g in games:
            if g["label"] == glabel:
                player_map[pname] = {"game": g["label"], "team": "", "abbr": "",
                                     "opp_sp": g["home_sp"], "pf": g["park_factor"]}
                break

    pool = set(list(hr_props.keys()) + list(player_map.keys()))
    cands = []
    for pname in pool:
        bsv  = _match(pname, sv_bat)  or {}
        bxst = _match(pname, xst_bat) or {}
        ginfo = _match(pname, player_map) or {}
        prop  = _match(pname, hr_props) or {}

        if not bsv and not prop:
            continue

        opp_sp  = ginfo.get("opp_sp") or {}
        sp_name = opp_sp.get("name", "") if isinstance(opp_sp, dict) else ""
        psv_raw = _match(sp_name, sv_pit) or {} if sp_name else {}
        pmlb    = psv_raw.pop("_mlb", {}) if "_mlb" in psv_raw else {}
        psv     = psv_raw
        pxst    = _match(sp_name, xst_pit) or {} if sp_name else {}

        pf = ginfo.get("pf", 1.0)
        sc, sigs = score_candidate(bsv, bxst, psv, pxst, pmlb, pf)
        zf = zone_fit(bsv, bxst)

        over_odds = prop.get("over_odds")
        implied   = american_to_implied(over_odds) if over_odds else None
        model_p   = min(0.92, max(0.04, sc / 26.0))
        edge      = round((model_p - implied) * 100, 1) if implied else None

        era = pmlb.get("era",0) or pxst.get("era",0)

        cands.append({
            "player":   pname,
            "team":     ginfo.get("team",""),
            "game":     ginfo.get("game", prop.get("game","")),
            "hr_score": sc,
            "zone_fit": zf,
            "park_factor": round(pf, 2),
            "has_prop": bool(prop),
            "signals":  sigs,
            "prop": {
                "line":        prop.get("line", 0.5),
                "over_odds":   over_odds,
                "under_odds":  prop.get("under_odds"),
                "implied_pct": round(implied * 100, 1) if implied else None,
                "model_pct":   round(model_p * 100, 1),
                "edge":        edge,
            } if prop else None,
            "statcast": {
                "ev":         bsv.get("ev",0),
                "ev50":       bsv.get("ev50",0),
                "la":         bsv.get("la",0),
                "barrel":     bsv.get("brl",0),
                "hard_hit":   bsv.get("hh",0),
                "sweet_spot": bsv.get("ss",0),
                "pull_pct":   bsv.get("pull",0),
                "oppo_pct":   bsv.get("oppo",0),
                "fb_pct":     bsv.get("fb",0),
                "gb_pct":     bsv.get("gb",0),
                "iso":        bxst.get("iso",0),
                "xwoba":      bxst.get("xwoba",0),
                "xslg":       bxst.get("xslg",0),
                "xba":        bxst.get("xba",0),
                "bip":        bxst.get("bip",0),
                "slg":        bxst.get("slg",0),
                "ba":         bxst.get("ba",0),
            },
            "pitcher": {
                "name":           sp_name,
                "era":            era,
                "hr_per_9":       pmlb.get("hr_per_9",0),
                "hr_allowed":     pmlb.get("hr_allowed",0),
                "ip":             pmlb.get("ip",0),
                "barrel_allowed": psv.get("brl",0),
                "ev_allowed":     psv.get("ev",0),
                "hh_allowed":     psv.get("hh",0),
                "xera":           pxst.get("xera",0),
                "xba_against":    pxst.get("xba",0),
            },
        })

    cands.sort(key=lambda c: (
        1 if c["has_prop"] else 0,
        (c["prop"] or {}).get("edge") or 0,
        c["hr_score"]
    ), reverse=True)
    return cands

# ── Refresh ───────────────────────────────────────────────────────────────────

def refresh(force=False):
    global _cache
    today = today_str()
    with _lock:
        if not force and _cache["date"] == today and _cache["candidates"]:
            return
        _cache["status"] = "refreshing"
        _cache["error"]  = None

    print(f"[Refresh] Starting for {today}")
    try:
        games    = fetch_games(today)
        hr_props = fetch_hr_props()
        sv_bat   = fetch_savant_ev("batter")
        sv_pit   = fetch_savant_ev("pitcher", min_bbe=30)
        xst_bat  = fetch_savant_xstats("batter")
        xst_pit  = fetch_savant_xstats("pitcher", min_pa=25)

        # Fetch MLB pitcher stats and inject into sv_pit
        for g in games:
            for key in ("home_sp", "away_sp"):
                sp = g[key]
                if sp.get("id") and sp.get("name"):
                    stats = fetch_pitcher_stats(sp["id"])
                    if stats:
                        sname = sp["name"]
                        if sname not in sv_pit:
                            sv_pit[sname] = {}
                        sv_pit[sname]["_mlb"] = stats

        cands = build_candidates(games, hr_props, sv_bat, sv_pit, xst_bat, xst_pit)

        with _lock:
            _cache.update({
                "date":         today,
                "candidates":   cands,
                "games":        len(games),
                "with_props":   sum(1 for c in cands if c["has_prop"]),
                "last_refresh": now_est().strftime("%I:%M %p ET"),
                "status":       "ready",
                "error":        None,
            })
        print(f"[Refresh] Done — {len(cands)} candidates, {len(hr_props)} props")
    except Exception as e:
        import traceback; traceback.print_exc()
        with _lock:
            _cache["status"] = "error"
            _cache["error"]  = str(e)

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/hr")
def api_hr():
    if _cache["status"] in ("idle",) or _cache["date"] != today_str():
        threading.Thread(target=refresh, daemon=True).start()
        return jsonify({"status": "loading", "message": "Fetching data — refresh in 15s"})
    if _cache["status"] == "refreshing":
        return jsonify({"status": "loading", "message": "Refresh in progress..."})
    if _cache["status"] == "error":
        return jsonify({"error": _cache.get("error","Unknown error")})
    return jsonify({
        "date":         now_est().strftime("%A, %B %d, %Y"),
        "last_refresh": _cache["last_refresh"],
        "games":        _cache["games"],
        "with_props":   _cache["with_props"],
        "total":        len(_cache["candidates"]),
        "candidates":   _cache["candidates"],
    })

@app.route("/api/refresh", methods=["GET","POST"])
def api_refresh():
    threading.Thread(target=refresh, args=(True,), daemon=True).start()
    return jsonify({"status": "refresh started"})

@app.route("/api/status")
def api_status():
    return jsonify({
        "status":       _cache["status"],
        "date":         _cache["date"],
        "total":        len(_cache["candidates"]),
        "with_props":   _cache["with_props"],
        "last_refresh": _cache["last_refresh"],
        "odds_api_set": bool(ODDS_API_KEY),
    })

# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>HR Finder — TwinPicks</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><text y='28' font-size='28'>🏠</text></svg>">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#06060a;--s:rgba(255,255,255,0.06);--b:rgba(255,255,255,0.1);--blue:#3b82f6;--green:#10b981;--red:#ef4444;--gold:#f59e0b;--text:#e8e8ec;--dim:#6b7280;--r:14px;--rs:9px}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
body::before{content:'';position:fixed;top:-20%;left:-10%;width:50%;height:50%;background:radial-gradient(circle,rgba(245,158,11,.06),transparent 70%);pointer-events:none;z-index:0}
body::after{content:'';position:fixed;bottom:-15%;right:-10%;width:40%;height:40%;background:radial-gradient(circle,rgba(239,68,68,.04),transparent 70%);pointer-events:none;z-index:0}
.nav{position:sticky;top:0;z-index:100;background:rgba(6,6,10,.9);backdrop-filter:blur(28px);border-bottom:1px solid var(--b);padding:10px 20px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.brand{font-size:16px;font-weight:900;display:flex;align-items:center;gap:8px}
.brand .tag{font-size:10px;font-weight:700;padding:2px 8px;border-radius:5px;background:rgba(245,158,11,.15);color:var(--gold)}
.nav-r{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.ftabs{display:flex;gap:3px;background:rgba(255,255,255,.05);padding:3px;border-radius:8px}
.ftab{padding:5px 12px;border:none;background:transparent;color:var(--dim);font-size:11px;font-weight:600;cursor:pointer;border-radius:5px;font-family:inherit;transition:.15s}
.ftab.on{background:linear-gradient(135deg,#f59e0b,#d97706);color:#000}
.btn{padding:7px 14px;border:none;border-radius:8px;font-weight:700;font-size:11px;cursor:pointer;font-family:inherit;transition:.15s}
.btn-gold{background:linear-gradient(135deg,#f59e0b,#d97706);color:#000}
.btn-gold:hover{transform:translateY(-1px);box-shadow:0 4px 14px rgba(245,158,11,.3)}
.btn-ghost{background:var(--s);color:var(--text);border:1px solid var(--b)}
.wrap{max-width:1320px;margin:0 auto;padding:16px 20px;position:relative;z-index:1}
.top{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:14px;flex-wrap:wrap;gap:8px}
.ttl{font-size:22px;font-weight:900;background:linear-gradient(135deg,var(--gold),#f87171);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.sub{font-size:11px;color:var(--dim);margin-top:3px}
.sbar{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px}
.sc{background:var(--s);border:1px solid var(--b);border-radius:var(--rs);padding:11px 13px}
.sl{font-size:9px;font-weight:700;color:var(--dim);text-transform:uppercase;letter-spacing:1px;margin-bottom:3px}
.sv{font-size:20px;font-weight:900;font-family:'JetBrains Mono'}
.stmsg{padding:9px 13px;border-radius:8px;font-size:12px;background:rgba(59,130,246,.08);color:var(--blue);border:1px solid rgba(59,130,246,.15);margin-bottom:12px;display:none}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:11px}
.card{background:var(--s);border:1px solid var(--b);border-radius:var(--r);padding:15px;transition:.18s;position:relative}
.card:hover{border-color:rgba(245,158,11,.2);box-shadow:0 0 22px rgba(245,158,11,.06)}
.card.hasp{border-left:3px solid var(--gold)}
.card.top{border-color:rgba(245,158,11,.35);background:rgba(245,158,11,.025)}
.ch{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:9px}
.pn{font-size:14px;font-weight:800}
.pm{font-size:10px;color:var(--dim);margin-top:2px}
.sb{text-align:right}
.sn{font-size:25px;font-weight:900;font-family:'JetBrains Mono';line-height:1}
.sl2{font-size:7px;color:var(--dim);letter-spacing:1px;text-transform:uppercase;margin-top:1px}
.bdg{display:flex;gap:4px;margin-bottom:8px;flex-wrap:wrap;align-items:center}
.b{font-size:9px;font-weight:800;padding:2px 7px;border-radius:5px}
.zA{background:rgba(16,185,129,.15);color:var(--green)}
.zBp{background:rgba(59,130,246,.15);color:#60a5fa}
.zB{background:rgba(59,130,246,.1);color:var(--blue)}
.zC{background:rgba(245,158,11,.12);color:var(--gold)}
.zD{background:rgba(239,68,68,.12);color:var(--red)}
.pf-g{background:rgba(16,185,129,.1);color:var(--green)}
.pf-b{background:rgba(239,68,68,.1);color:var(--red)}
.pf-n{background:rgba(255,255,255,.05);color:var(--dim)}
.b-prop{background:rgba(245,158,11,.12);color:var(--gold)}
.pbox{background:rgba(245,158,11,.05);border:1px solid rgba(245,158,11,.15);border-radius:8px;padding:7px 10px;margin-bottom:7px}
.pl{display:flex;align-items:center;justify-content:space-between}
.po{display:flex;gap:8px;font-family:'JetBrains Mono';font-size:11px;margin-top:3px}
.pibox{background:rgba(239,68,68,.05);border:1px solid rgba(239,68,68,.12);border-radius:8px;padding:6px 10px;margin-bottom:7px;font-size:10px}
.pin{font-weight:700;color:#f87171;margin-bottom:3px}
.pis{display:flex;gap:8px;flex-wrap:wrap;color:var(--dim)}
.srow{display:grid;gap:4px;margin-bottom:4px}
.s4{grid-template-columns:repeat(4,1fr)}.s3{grid-template-columns:repeat(3,1fr)}
.sbox{background:rgba(255,255,255,.03);border-radius:7px;padding:5px 3px;text-align:center}
.slbl{font-size:8px;font-weight:700;color:var(--dim);letter-spacing:.4px;margin-bottom:2px}
.sval{font-size:13px;font-weight:800;font-family:'JetBrains Mono'}
.sigs{margin-top:6px}
.sig{font-size:9px;padding:1px 0;line-height:1.4}
.sg{color:var(--green)}.sr{color:var(--red)}.sn2{color:var(--dim)}
.empty{text-align:center;padding:60px 20px;color:var(--dim);font-size:14px}
.spin{display:inline-block;animation:sp 1s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
footer{text-align:center;padding:20px;font-size:10px;color:rgba(255,255,255,.12)}
::-webkit-scrollbar{width:5px}::-webkit-scrollbar-thumb{background:rgba(255,255,255,.08);border-radius:3px}
@media(max-width:600px){.sbar{grid-template-columns:repeat(2,1fr)}.s4{grid-template-columns:repeat(2,1fr)}}
</style></head><body>
<div class="nav">
  <div class="brand">⚾ HR Finder <span class="tag">TWINPICKS</span></div>
  <div class="nav-r">
    <div class="ftabs">
      <button class="ftab on" data-f="props">PROPS ONLY</button>
      <button class="ftab" data-f="all">ALL PLAYERS</button>
    </div>
    <button class="btn btn-gold" onclick="doRefresh()">↺ Refresh</button>
  </div>
</div>
<div class="wrap">
  <div class="top">
    <div><div class="ttl">Home Run Finder</div><div class="sub" id="sub"></div></div>
    <div id="lr" style="font-size:10px;color:var(--dim)"></div>
  </div>
  <div class="sbar" id="sbar"></div>
  <div class="stmsg" id="stmsg"></div>
  <div id="out"><div class="empty"><span class="spin">⚾</span><br><br>Loading HR data...</div></div>
</div>
<footer>TwinPicks HR Finder — Statcast · Starting Pitcher · Sportsbook</footer>
<script>
var D=null,filt='props',_pt=null;
document.querySelectorAll('.ftab').forEach(function(b){b.addEventListener('click',function(){
  document.querySelectorAll('.ftab').forEach(function(x){x.classList.remove('on')});
  b.classList.add('on');filt=b.dataset.f;if(D)render(D);
})});
function N(v,d){var f=parseFloat(v);return isNaN(f)?0:(d!==undefined?f.toFixed(d):f)}
function f3(v){var f=N(v);return f>0?'.'+String(Math.round(f*1000)).padStart(3,'0'):'—'}
function fOdds(v){return v>0?'+'+v:(v<0?String(v):'—')}
function cEV(v){return N(v)>=91?'c-green':N(v)>=89?'c-gold':N(v)>0&&N(v)<86?'c-red':''}
function cBrl(v){return N(v)>=12?'c-green':N(v)>=8?'c-gold':''}
function cLA(v){var la=N(v);return la>=18&&la<=28?'c-green':la>=14&&la<=32?'c-gold':la>0?'c-red':''}
function cHH(v){return N(v)>=48?'c-green':N(v)>=42?'c-gold':''}
function cISO(v){return N(v)>=0.220?'c-green':N(v)>=0.170?'c-gold':''}
function cXW(v){return N(v)>=0.380?'c-green':N(v)>=0.320?'c-gold':N(v)>0&&N(v)<0.280?'c-red':''}
function cPull(v){return N(v)>=48?'c-green':N(v)>=40?'c-gold':''}
function cFB(v){return N(v)>=44?'c-green':N(v)>=36?'c-gold':''}
function cSS(v){return N(v)>=38?'c-green':N(v)>=28?'c-gold':''}
function cGB(v){return N(v)>=55?'c-red':N(v)>=45?'c-gold':''}
function cScore(v){return N(v)>=12?'var(--green)':N(v)>=8?'var(--gold)':N(v)>=4?'var(--text)':'var(--dim)'}
function zbcls(z){return z==='A'?'zA':z==='B+'?'zBp':z==='B'?'zB':z==='C'?'zC':'zD'}
function css(c){return c?(' class="'+c+'"'):''} // helper for coloring
function box(lbl,val,c){return'<div class="sbox"><div class="slbl">'+lbl+'</div><div class="sval'+(c?' '+c:'')+'">'+(val||'—')+'</div></div>'}

function render(d){
  var cands=d.candidates||[];
  if(filt==='props')cands=cands.filter(function(c){return c.has_prop});
  var sb='';
  sb+='<div class="sc"><div class="sl">Games</div><div class="sv">'+(d.games||0)+'</div></div>';
  sb+='<div class="sc"><div class="sl">With Props</div><div class="sv" style="color:var(--gold)">'+(d.with_props||0)+'</div></div>';
  sb+='<div class="sc"><div class="sl">Analyzed</div><div class="sv">'+(d.total||0)+'</div></div>';
  sb+='<div class="sc"><div class="sl">Showing</div><div class="sv">'+cands.length+'</div></div>';
  document.getElementById('sbar').innerHTML=sb;
  document.getElementById('sub').textContent=d.date||'';
  document.getElementById('lr').textContent=d.last_refresh?'Refreshed '+d.last_refresh:'';
  if(!cands.length){document.getElementById('out').innerHTML='<div class="empty">'+(filt==='props'?'No HR props loaded. Add ODDS_API_KEY or switch to All Players.':'No candidates today.')+'</div>';return}
  var h='<div class="grid">';
  for(var i=0;i<cands.length;i++){
    var c=cands[i],st=c.statcast||{},pr=c.prop,pi=c.pitcher||{},sc=c.hr_score;
    var isTop=sc>=12&&c.has_prop;
    h+='<div class="card'+(c.has_prop?' hasp':'')+(isTop?' top':'')+'">';
    // Header
    h+='<div class="ch"><div><div class="pn">'+c.player+'</div><div class="pm">'+(c.team||''+(c.game?' · '+c.game:''))+'</div></div>';
    h+='<div class="sb"><div class="sn" style="color:'+cScore(sc)+'">'+sc+'</div><div class="sl2">HR Score</div></div></div>';
    // Badges
    h+='<div class="bdg">';
    var zf=c.zone_fit||'?';h+='<span class="b '+zbcls(zf)+'">ZONE '+zf+'</span>';
    var pfc=c.park_factor>=1.05?'pf-g':c.park_factor<=0.95?'pf-b':'pf-n';
    h+='<span class="b '+pfc+'">PF '+N(c.park_factor,2)+'</span>';
    if(c.has_prop)h+='<span class="b b-prop">SPORTSBOOK</span>';
    if(pr&&pr.edge!==null&&pr.edge!==undefined){var ec=pr.edge>3?'var(--green)':pr.edge>0?'var(--gold)':'var(--red)';h+='<span class="b" style="background:rgba(255,255,255,.05);color:'+ec+'">'+(pr.edge>0?'+':'')+pr.edge+'% edge</span>'}
    h+='</div>';
    // Prop
    if(pr){
      h+='<div class="pbox"><div class="pl"><span style="font-size:12px;font-weight:800;color:var(--gold)">HR '+N(pr.line,1)+'</span>'+(pr.implied_pct?'<span style="font-size:10px;color:var(--dim)">Implied: '+pr.implied_pct+'%</span>':'')+'</div>';
      h+='<div class="po">';
      if(pr.over_odds)h+='<span style="color:var(--green)">O '+fOdds(pr.over_odds)+'</span>';
      if(pr.under_odds)h+='<span style="color:var(--red)">U '+fOdds(pr.under_odds)+'</span>';
      h+='<span style="color:var(--dim)">Model: '+N(pr.model_pct,0)+'%</span>';
      h+='</div></div>';
    }
    // Pitcher
    if(pi.name){
      h+='<div class="pibox"><div class="pin">vs '+pi.name+'</div><div class="pis">';
      if(pi.era)h+='<span>'+N(pi.era,2)+' ERA</span>';
      if(pi.hr_per_9)h+='<span style="color:'+(N(pi.hr_per_9)>=1.5?'var(--red)':N(pi.hr_per_9)>=1.0?'var(--gold)':'inherit')+'">'+N(pi.hr_per_9,2)+' HR/9</span>';
      if(pi.hr_allowed)h+='<span>'+pi.hr_allowed+' HR allowed</span>';
      if(pi.ip)h+='<span>'+N(pi.ip,1)+' IP</span>';
      if(pi.barrel_allowed)h+='<span>'+N(pi.barrel_allowed,1)+'% BRL all.</span>';
      if(pi.xera)h+='<span>'+N(pi.xera,2)+' xERA</span>';
      h+='</div></div>';
    }
    // Row 1: Raw power
    h+='<div class="srow s4">';
    h+=box('EV',N(st.ev)>0?N(st.ev,1):'—',cEV(st.ev));
    h+=box('BRL%',N(st.barrel)>0?N(st.barrel,1)+'%':'—',cBrl(st.barrel));
    h+=box('LA°',N(st.la)!==0?N(st.la,0)+'°':'—',cLA(st.la));
    h+=box('HH%',N(st.hard_hit)>0?N(st.hard_hit,0)+'%':'—',cHH(st.hard_hit));
    h+='</div>';
    // Row 2: Expected / quality
    h+='<div class="srow s4">';
    h+=box('ISO',N(st.iso)>0?f3(st.iso):'—',cISO(st.iso));
    h+=box('xwOBA',N(st.xwoba)>0?f3(st.xwoba):'—',cXW(st.xwoba));
    h+=box('xSLG',N(st.xslg)>0?f3(st.xslg):'—',N(st.xslg)>=0.500?'c-green':N(st.xslg)>=0.400?'c-gold':'');
    h+=box('BIP',N(st.bip)>0?st.bip:'—','');
    h+='</div>';
    // Row 3: Spray / batted ball
    h+='<div class="srow s4">';
    h+=box('PULL%',N(st.pull_pct)>0?N(st.pull_pct,0)+'%':'—',cPull(st.pull_pct));
    h+=box('FB%',N(st.fb_pct)>0?N(st.fb_pct,0)+'%':'—',cFB(st.fb_pct));
    h+=box('SS%',N(st.sweet_spot)>0?N(st.sweet_spot,0)+'%':'—',cSS(st.sweet_spot));
    h+=box('GB%',N(st.gb_pct)>0?N(st.gb_pct,0)+'%':'—',cGB(st.gb_pct));
    h+='</div>';
    // Signals
    if(c.signals&&c.signals.length){h+='<div class="sigs">';for(var s=0;s<Math.min(c.signals.length,4);s++){var sg=c.signals[s];var sgc=sg.indexOf('unlikely')>=0||sg.indexOf('Low EV')>=0||sg.indexOf('Pitcher park')>=0||sg.indexOf('suppressor')>=0?'sr':'sg';h+='<div class="sig '+sgc+'">● '+sg+'</div>'}h+='</div>'}
    h+='</div>';
  }
  h+='</div>';
  document.getElementById('out').innerHTML=h;
}

function doRefresh(){
  var st=document.getElementById('stmsg');st.style.display='block';st.textContent='Refreshing all data...';
  fetch('/api/refresh',{method:'POST'}).then(function(){setTimeout(load,2000)});
}

function load(){
  fetch('/api/hr').then(function(r){return r.json()}).then(function(d){
    if(d.status==='loading'){
      var st=document.getElementById('stmsg');st.style.display='block';st.textContent=d.message||'Loading...';
      if(!_pt)_pt=setInterval(load,5000);return;
    }
    if(_pt){clearInterval(_pt);_pt=null}
    document.getElementById('stmsg').style.display='none';
    if(d.error){document.getElementById('out').innerHTML='<div class="empty">'+d.error+'</div>';return}
    D=d;render(d);
  }).catch(function(){document.getElementById('out').innerHTML='<div class="empty">Error. <a href="javascript:load()" style="color:var(--blue)">Retry</a></div>'});
}
load();
// CSS color classes for stat values
document.head.insertAdjacentHTML('beforeend','<style>.c-green{color:var(--green)}.c-gold{color:var(--gold)}.c-red{color:var(--red)}.c-blue{color:var(--blue)}</style>');
</script></body></html>"""

# ── Boot ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    threading.Thread(target=refresh, daemon=True).start()
    app.run(host="0.0.0.0", port=port, debug=False)
