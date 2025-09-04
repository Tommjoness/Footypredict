# mock_api.py
import os
import math
import json
import datetime as dt
from typing import List, Dict, Any, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

# ------------------------------------------------------------
# Config
# ------------------------------------------------------------
FD_BASE = "https://api.football-data.org/v4"
FD_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "")
CRON_TOKEN = os.getenv("CRON_TOKEN", "")
COMP_CODES = ["DED", "PL", "PD", "BL1", "FL1"]  # Eredivisie, Premier League, La Liga, Bundesliga, Ligue 1

# We cachen per dag zodat /refresh de data kan “voorwarmen”
CACHE: Dict[str, List[Dict[str, Any]]] = {}

app = FastAPI(title="FootyPredict Mock API", version="2.0")

# ------------------------------------------------------------
# Utilities
# ------------------------------------------------------------

def _iso(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")

def _one_dec(x: float) -> float:
    return round(x + 1e-9, 1)

def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))

def _poisson_p(k: int, lam: float) -> float:
    # P(X=k) for Poisson(lam)
    return (lam ** k) * math.exp(-lam) / math.factorial(k)

def _prob_overs(total_lambda: float, line: float) -> float:
    # benadering: voor O1.5 / O2.5 / O3.5 tel P(T>=ceil(line+epsilon)) met Poisson(total_lambda)
    # voor 1.5 => k >= 2, 2.5 => k >= 3, 3.5 => k >= 4
    kmin = int(math.floor(line + 1.0))  # 1.5->2, 2.5->3, 3.5->4
    return 1.0 - sum(_poisson_p(k, total_lambda) for k in range(0, kmin))

def _prob_btts(lh: float, la: float) -> float:
    # BTTS = 1 - P(home=0 or away=0) + P(both 0)
    p0h = _poisson_p(0, lh)
    p0a = _poisson_p(0, la)
    both0 = p0h * p0a
    return 1.0 - (p0h + p0a - both0)

def _one_x_two(lh: float, la: float, g_cap: int = 6) -> Dict[str, float]:
    # Simuleer uitslagen 0..g_cap-1 voor home & away met onafhankelijke Poisson
    p_home = 0.0
    p_draw = 0.0
    p_away = 0.0
    for gh in range(g_cap):
        ph = _poisson_p(gh, lh)
        for ga in range(g_cap):
            pa = _poisson_p(ga, la)
            pij = ph * pa
            if gh > ga:
                p_home += pij
            elif gh == ga:
                p_draw += pij
            else:
                p_away += pij
    # normaliseren (numeriek)
    s = p_home + p_draw + p_away
    if s > 0:
        p_home, p_draw, p_away = p_home/s, p_draw/s, p_away/s
    return {"p_home": p_home, "p_draw": p_draw, "p_away": p_away}

def _top_correct_scores(lh: float, la: float, topn: int = 5, g_cap: int = 6) -> List[Dict[str, Any]]:
    grid = []
    for gh in range(g_cap):
        ph = _poisson_p(gh, lh)
        for ga in range(g_cap):
            pa = _poisson_p(ga, la)
            grid.append( (gh, ga, ph*pa) )
    grid.sort(key=lambda x: x[2], reverse=True)
    out = []
    tot = sum(p for _,_,p in grid)
    for gh,ga,p in grid[:topn]:
        pct = int(round(100 * p / (tot + 1e-12)))
        out.append({"score": f"{gh}-{ga}", "p": pct/100.0})
    return out

# ------------------------------------------------------------
# Football-Data helpers
# ------------------------------------------------------------

def _fd_headers() -> Dict[str, str]:
    if not FD_KEY:
        raise HTTPException(status_code=500, detail="FOOTBALL_DATA_API_KEY missing")
    return {"X-Auth-Token": FD_KEY}

async def fd_get(client: httpx.AsyncClient, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    r = await client.get(f"{FD_BASE}{path}", headers=_fd_headers(), params=params, timeout=30.0)
    r.raise_for_status()
    return r.json()

async def fetch_matches_for_date(client: httpx.AsyncClient, date_str: str) -> List[Dict[str, Any]]:
    # /matches?competitions=PL,BL1,...&dateFrom=YYYY-MM-DD&dateTo=YYYY-MM-DD
    data = await fd_get(client, "/matches", {
        "competitions": ",".join(COMP_CODES),
        "dateFrom": date_str,
        "dateTo": date_str,
        "status": "SCHEDULED,IN_PLAY,PAUSED,FINISHED",
        "limit": 200,
    })
    matches = data.get("matches", []) or []
    return matches

async def last3_avg_goals(client: httpx.AsyncClient, team_id: int) -> float:
    # Laatste 3 gespeelde matches van dit team
    data = await fd_get(client, f"/teams/{team_id}/matches", {
        "status": "FINISHED",
        "limit": 3
    })
    ms = data.get("matches", []) or []
    goals = []
    for m in ms:
        home = m["homeTeam"]["id"] == team_id
        score = m.get("score", {}).get("fullTime", {}) or {}
        gh = score.get("home", 0) or 0
        ga = score.get("away", 0) or 0
        goals_scored = gh if home else ga
        goals.append(goals_scored)
    if not goals:
        return 1.2  # neutraal fallback
    return _one_dec(sum(goals) / len(goals))

# ------------------------------------------------------------
# Core: bouwen van onze voorspellingstructuur voor Flutter
# ------------------------------------------------------------

async def build_predictions_for_date(date_str: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    async with httpx.AsyncClient() as client:
        matches = await fetch_matches_for_date(client, date_str)

        # Als niets: geef lege lijst terug (Flutter kan dan "Geen wedstrijden" tonen)
        if not matches:
            return []

        for m in matches:
            try:
                comp = (m.get("competition") or {}).get("code") or ""
                home = (m.get("homeTeam") or {}).get("name") or "Home"
                away = (m.get("awayTeam") or {}).get("name") or "Away"
                hid = (m.get("homeTeam") or {}).get("id")
                aid = (m.get("awayTeam") or {}).get("id")

                # Gemiddelde goals laatste 3 (per team)
                avg_h = await last3_avg_goals(client, hid) if hid else 1.2
                avg_a = await last3_avg_goals(client, aid) if aid else 1.2

                # Expected goals voor de wedstrijd (heel simpel model)
                # kleine home advantage factor
                lam_h = max(0.3, avg_h + 0.2)
                lam_a = max(0.3, avg_a)

                one_x_two_p = _one_x_two(lam_h, lam_a)
                total_lambda = lam_h + lam_a

                p_btts = _prob_btts(lam_h, lam_a)
                p_o15 = _prob_overs(total_lambda, 1.5)
                p_o25 = _prob_overs(total_lambda, 2.5)
                p_o35 = _prob_overs(total_lambda, 3.5)

                top_scores = _top_correct_scores(lam_h, lam_a, topn=5, g_cap=6)

                out.append({
                    "home_team": home,
                    "away_team": away,
                    "league": comp,         # code (DED/PL/…); je UI toont de league-naam al afzonderlijk
                    "date": date_str,
                    "p_home": round(one_x_two_p["p_home"], 2),
                    "p_draw": round(one_x_two_p["p_draw"], 2),
                    "p_away": round(one_x_two_p["p_away"], 2),
                    "p_btts_yes": round(p_btts, 2),
                    "p_over_15": round(p_o15, 2),
                    "p_over_25": round(p_o25, 2),
                    "p_over_35": round(p_o35, 2),
                    "avg_goals_home_last3": avg_h,
                    "avg_goals_away_last3": avg_a,
                    "top_scores": top_scores,
                })
            except Exception:
                # Bij een enkele failure: sla die match over i.p.v. alles stuk te maken
                continue
    return out

# ------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------

@app.get("/")
async def root():
    return {"ok": True, "service": "FootyPredict Mock API", "utc": dt.datetime.utcnow().isoformat()}

@app.get("/predictions")
async def predictions(d: Optional[str] = None):
    """
    GET /predictions?d=YYYY-MM-DD  (default: vandaag)
    """
    if d is None:
        d = _iso(dt.date.today())

    # cache check
    if d in CACHE:
        return JSONResponse(CACHE[d])

    try:
        data = await build_predictions_for_date(d)
    except httpx.HTTPStatusError as e:
        # doorgeven met nette melding
        raise HTTPException(status_code=e.response.status_code, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    CACHE[d] = data
    return JSONResponse(data)

@app.post("/refresh")
async def refresh(request: Request, x_cron_token: Optional[str] = Header(None)):
    """
    POST /refresh
    Protected door header: X-CRON-TOKEN: <CRON_TOKEN>
    Vult vandaag (en optioneel morgen) in de cache.
    """
    if not CRON_TOKEN:
        raise HTTPException(status_code=500, detail="CRON_TOKEN env missing on server")
    if x_cron_token != CRON_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")

    today = _iso(dt.date.today())
    try:
        CACHE[today] = await build_predictions_for_date(today)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"refresh failed: {e}")

    return {"ok": True, "cached_dates": [k for k in CACHE.keys() if k == today]}

# Render start command expects: uvicorn mock_api:app --host 0.0.0.0 --port $PORT
