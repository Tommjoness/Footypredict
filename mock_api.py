# mock_api.py
import os
from datetime import date, datetime
from functools import lru_cache
from typing import Dict, List, Any

import httpx
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="FootyPredict Mock API", version="1.0")

# CORS: sta alles toe (voor testen / jouw Flutter web)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----- CONFIG -----
LEAGUE_NAMES = {
    "Eredivisie": "Dutch Eredivisie",
    "Premier League": "English Premier League",
    "La Liga": "Spanish La Liga",
    "Bundesliga": "German Bundesliga",
    "Ligue 1": "French Ligue 1",
}
THESPORTSDB_BASE = "https://www.thesportsdb.com/api/v1/json/3"

def _one_dec(n: float) -> float:
    return round(float(n), 1)

# Demo data voor je huidige kaarten (blijft werken)
SAMPLE = [
    {
        "home_team": "Ajax",
        "away_team": "Feyenoord",
        "league": "Eredivisie",
        "date": "2025-09-03",
        "p_home": 0.45,
        "p_draw": 0.26,
        "p_away": 0.29,
        "p_btts_yes": 0.53,
        "p_over_15": 0.74,
        "p_over_25": 0.50,
        "p_over_35": 0.28,
        "avg_goals_home_last3": 3.7,
        "avg_goals_away_last3": 2.3,
        "top_scores": [
            {"score": "2-1", "p": 0.18},
            {"score": "1-1", "p": 0.16},
            {"score": "2-0", "p": 0.12},
            {"score": "3-1", "p": 0.11},
            {"score": "1-2", "p": 0.10},
        ],
    }
]

@app.get("/predictions")
def predictions(d: str | None = Query(None, description="YYYY-MM-DD")) -> List[Dict[str, Any]]:
    """
    Je bestaande endpoint. Laat mock data zien (zoals nu), zodat de Flutter UI blijft werken.
    Optioneel ?d=YYYY-MM-DD wordt genegeerd in de mock.
    """
    return SAMPLE

# ---------- Fixtures: live per dag vanuit TheSportsDB (gratis, geen key nodig) ----------

@lru_cache(maxsize=128)
def _cached_key(iso_date: str) -> str:
    # simpele cache key helper
    return f"fixtures::{iso_date}"

async def fetch_league_fixtures(iso_date: str, league_public_name: str) -> List[Dict[str, Any]]:
    """
    Haal fixtures van 1 competitie voor een specifieke dag op via TheSportsDB.
    Documentatie: /eventsday.php?d=YYYY-MM-DD&l=League%20Name
    """
    league_query = LEAGUE_NAMES[league_public_name]
    url = f"{THESPORTSDB_BASE}/eventsday.php"
    params = {"d": iso_date, "l": league_query}

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()

    events = data.get("events") or []
    fixtures: List[Dict[str, Any]] = []
    for ev in events:
        # fields zijn niet altijd volledig; defensief parsen
        fixtures.append({
            "home": ev.get("strHomeTeam"),
            "away": ev.get("strAwayTeam"),
            "time": ev.get("strTime") or ev.get("strTimestamp"),
            "venue": ev.get("strVenue"),
        })
    return fixtures

@app.get("/fixtures")
async def fixtures(d: str | None = Query(None, description="YYYY-MM-DD")) -> Dict[str, Any]:
    """
    Retourneert per competitie de wedstrijden van die dag.
    Als er geen wedstrijden zijn → lege lijst [] (Flutter toont 'geen wedstrijden').
    """
    iso_date = d or date.today().isoformat()

    out: Dict[str, Any] = {"date": iso_date, "leagues": {}}
    for league in LEAGUE_NAMES.keys():
        try:
            items = await fetch_league_fixtures(iso_date, league)
        except Exception:
            # bij een fout: lever lege lijst terug (dan toont de UI 'geen wedstrijden')
            items = []
        out["leagues"][league] = items
    return out

# --- healthcheck voor Render ---
@app.get("/")
def root():
    return {"ok": True, "service": "FootyPredict Mock API", "utc": datetime.utcnow().isoformat()}
