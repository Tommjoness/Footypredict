import os
import json
from datetime import datetime, date
from typing import Dict, Any, List

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

# ------------------------------
# Config
# ------------------------------
API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "").strip()
CRON_TOKEN = os.getenv("CRON_TOKEN", "").strip()

# Football-Data v4 competition codes
WANTED_CODES = {
    "DED": "Eredivisie",
    "PL": "Premier League",
    "PD": "La Liga",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
}

FD_BASE = "https://api.football-data.org/v4"

# Simpele default "mock" kansen (tot je eigen model klaar is)
DEFAULT_PROBS = {
    "p_home": 0.45,
    "p_draw": 0.26,
    "p_away": 0.29,
    "p_btts_yes": 0.53,
    "p_over_15": 0.74,
    "p_over_25": 0.50,
    "p_over_35": 0.28,
}
DEFAULT_TOP_SCORES = [
    {"score": "2-1", "p": 0.18},
    {"score": "1-1", "p": 0.16},
    {"score": "2-0", "p": 0.12},
    {"score": "3-1", "p": 0.11},
    {"score": "1-2", "p": 0.10},
]

# Eenvoudige in-memory cache per datum
CACHE: Dict[str, List[Dict[str, Any]]] = {}

# ------------------------------
# App
# ------------------------------
app = FastAPI(title="FootyPredict Mock API (live fixtures)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ------------------------------
# Helpers
# ------------------------------
def _today_yyyy_mm_dd() -> str:
    return date.today().isoformat()  # Render gebruikt UTC; prima voor dag-vraag

def _fetch_matches_for_date(d_str: str) -> List[Dict[str, Any]]:
    """
    Haal alle matches op voor een specifieke datum en filter op gewenste competities.
    """
    if not API_KEY:
        # Zonder key kunnen we niets ophalen; lever lege lijst
        return []

    url = f"{FD_BASE}/matches"
    headers = {"X-Auth-Token": API_KEY}
    params = {"dateFrom": d_str, "dateTo": d_str}

    r = requests.get(url, headers=headers, params=params, timeout=20)
    if r.status_code == 429:
        # Rate limited / gratis plan: geef netjes leeg terug
        return []
    if r.status_code >= 400:
        # Iets anders mis – fail soft met lege lijst
        return []

    payload = r.json()
    matches = payload.get("matches", [])

    out: List[Dict[str, Any]] = []
    for m in matches:
        comp = m.get("competition", {}) or {}
        code = comp.get("code")
        if code not in WANTED_CODES:
            continue

        home = (m.get("homeTeam", {}) or {}).get("name", "Home")
        away = (m.get("awayTeam", {}) or {}).get("name", "Away")
        utc_date = (m.get("utcDate") or "")[:10] or d_str

        # Bouw record in het formaat dat je Flutter verwacht.
        item = {
            "home_team": home,
            "away_team": away,
            "league": WANTED_CODES.get(code, code or "League"),
            "date": utc_date,
            # Simpele default-kansen zolang je nog geen eigen model gebruikt
            **DEFAULT_PROBS,
            # Laatste 3 gemiddelden: nog niet beschikbaar -> None zodat je app ze kan verbergen
            "avg_goals_home_last3": None,
            "avg_goals_away_last3": None,
            # Top correct scores (placeholder set)
            "top_scores": DEFAULT_TOP_SCORES,
        }
        out.append(item)

    # Als er voor al onze gewenste competities géén wedstrijden zijn:
    # geef voor elke competitie een "geen wedstrijden" blok terug (zodat de app iets kan tonen)
    if not out:
        for code, name in WANTED_CODES.items():
            out.append({
                "home_team": "",
                "away_team": "",
                "league": name,
                "date": d_str,
                **DEFAULT_PROBS,
                "avg_goals_home_last3": None,
                "avg_goals_away_last3": None,
                "top_scores": [],
                "no_matches": True,   # hint voor frontend
            })

    return out

def _get_predictions(d_str: str) -> List[Dict[str, Any]]:
    # Cache per datum
    if d_str in CACHE:
        return CACHE[d_str]
    data = _fetch_matches_for_date(d_str)
    CACHE[d_str] = data
    return data

# ------------------------------
# Routes
# ------------------------------
@app.get("/")
def root():
    return {
        "ok": True,
        "service": "FootyPredict Mock API (live fixtures)",
        "utc": datetime.utcnow().isoformat(),
    }

@app.get("/predictions")
def predictions(d: str | None = None):
    """
    Voorbeeld:
      /predictions            -> vandaag
      /predictions?d=2025-09-04
    """
    d_str = (d or _today_yyyy_mm_dd()).strip()
    try:
        # valideer datum
        datetime.strptime(d_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Use d=YYYY-MM-DD")

    data = _get_predictions(d_str)
    return JSONResponse(data)

@app.post("/refresh")
def refresh(request: Request):
    """
    Wordt 1x per dag aangeroepen door jouw gratis GitHub Actions workflow.
    (Beschermd met optionele X-CRON-TOKEN header.)
    """
    if CRON_TOKEN:
        token = request.headers.get("X-CRON-TOKEN", "")
        if token != CRON_TOKEN:
            raise HTTPException(status_code=401, detail="Invalid cron token")

    # Prefetch vandaag (en eventueel uitbreiden met morgen/overmorgen)
    d_str = _today_yyyy_mm_dd()
    data = _get_predictions(d_str)
    return {"ok": True, "prefetched_date": d_str, "count": len(data)}
