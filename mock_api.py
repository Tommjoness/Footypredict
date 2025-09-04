# mock_api.py
import os
import hashlib
import random
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any

import uvicorn
from fastapi import FastAPI, Query, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx

app = FastAPI(title="FootyPredict API", version="3.0")

# Sta alles toe (Flutter web kan fetchen)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Config
CRON_TOKEN = os.getenv("CRON_TOKEN", "").strip()
API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "").strip()  # optioneel
SERVICE_TZ = timezone.utc

# Competitie-codes football-data.org
COMP_CODES = {
    "Eredivisie": "DED",
    "Premier League": "PL",
    "La Liga": "PD",
    "Bundesliga": "BL1",
    "Ligue 1": "FL1",
}

# Cache: { "YYYY-MM-DD": [prediction, ...] }
CACHE: Dict[str, List[Dict[str, Any]]] = {}

# ---------- Helpers ----------

def _today_str() -> str:
    return datetime.now(SERVICE_TZ).strftime("%Y-%m-%d")

def _rng(seed_key: str) -> random.Random:
    h = hashlib.sha256(seed_key.encode()).hexdigest()
    return random.Random(int(h[:16], 16))

def _pct(x: float) -> float:
    return round(max(0.0, min(1.0, x)), 3)

def _mock_prediction(league: str, date_iso: str, home: str, away: str) -> Dict[str, Any]:
    rng = _rng(f"{league}|{date_iso}|{home}|{away}")
    p_home = _pct(0.4 + 0.1 * rng.random())
    p_away = _pct(0.3 + 0.1 * rng.random())
    p_draw = _pct(1.0 - p_home - p_away)
    return {
        "home_team": home,
        "away_team": away,
        "league": league,
        "date": date_iso,
        "p_home": p_home,
        "p_draw": p_draw,
        "p_away": p_away,
        "p_btts_yes": _pct(0.5 + 0.2 * rng.random()),
        "p_over_15": _pct(0.65 + 0.2 * rng.random()),
        "p_over_25": _pct(0.47 + 0.18 * rng.random()),
        "p_over_35": _pct(0.25 + 0.2 * rng.random()),
        "avg_goals_home_last3": round(1.5 + rng.random() * 1.5, 1),
        "avg_goals_away_last3": round(1.5 + rng.random() * 1.5, 1),
        "top_scores": [
            {"score": "2-1", "p": 0.18},
            {"score": "1-1", "p": 0.15},
            {"score": "2-0", "p": 0.12},
        ],
    }

async def _fetch_from_api(date_iso: str) -> List[Dict[str, Any]]:
    """Probeer echte fixtures via football-data.org als er een API key is."""
    if not API_KEY:
        return []
    url = "https://api.football-data.org/v4/matches"
    params = {
        "dateFrom": date_iso,
        "dateTo": date_iso,
        "competitions": ",".join(COMP_CODES.values())
    }
    headers = {"X-Auth-Token": API_KEY}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, params=params, headers=headers)
        if r.status_code != 200:
            return []
        matches = r.json().get("matches", [])
    preds = []
    for m in matches:
        league = next((name for name, code in COMP_CODES.items() if code == m["competition"]["code"]), "Unknown")
        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]
        preds.append(_mock_prediction(league, date_iso, home, away))
    return preds

def _mock_fallback(date_iso: str) -> List[Dict[str, Any]]:
    """Gebruik vaste teams om altijd iets terug te geven."""
    teams = {
        "Eredivisie": [("Ajax", "Feyenoord")],
        "Premier League": [("Arsenal", "Chelsea")],
        "La Liga": [("Real Madrid", "Barcelona")],
        "Bundesliga": [("Bayern", "Dortmund")],
        "Ligue 1": [("PSG", "Marseille")],
    }
    out = []
    for league, pairs in teams.items():
        home, away = pairs[0]
        out.append(_mock_prediction(league, date_iso, home, away))
    return out

async def _get_predictions(date_iso: str) -> List[Dict[str, Any]]:
    if date_iso in CACHE:
        return CACHE[date_iso]
    preds = await _fetch_from_api(date_iso)
    if not preds:
        preds = _mock_fallback(date_iso)
    CACHE[date_iso] = preds
    return preds

async def _warm(days: int = 1):
    today = datetime.now(SERVICE_TZ)
    for i in range(days + 1):
        d = (today + timedelta(days=i)).strftime("%Y-%m-%d")
        await _get_predictions(d)

# ---------- Endpoints ----------

@app.get("/")
def root():
    return {"ok": True, "service": "FootyPredict API", "time": datetime.now(SERVICE_TZ).isoformat()}

@app.get("/predictions")
async def predictions(d: str | None = Query(None, description="YYYY-MM-DD")):
    date_iso = d or _today_str()
    return await _get_predictions(date_iso)

@app.post("/refresh")
async def refresh(x_cron_token: str | None = Header(None, convert_underscores=False)):
    if not CRON_TOKEN:
        raise HTTPException(status_code=500, detail="CRON_TOKEN not configured")
    if x_cron_token != CRON_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
    await _warm(days=3)
    return {"ok": True, "warmed": True}

@app.on_event("startup")
async def startup_event():
    await _warm(days=1)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8888"))
    uvicorn.run(app, host="0.0.0.0", port=port)
