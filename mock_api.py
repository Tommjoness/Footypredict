# mock_api.py
# FastAPI + Uvicorn app voor FootyPredict
# - /predictions?d=YYYY-MM-DD   -> lijst met voorspellingen (5 topcompetities)
# - /refresh (POST, met X-CRON-TOKEN) -> warmt de cache per dag op
# - automatische fallback naar mock fixtures als geen API key of API faalt

import os
import json
import hashlib
import random
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Tuple

import uvicorn
from fastapi import FastAPI, Query, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx

app = FastAPI(title="FootyPredict Mock API", version="2.0")

# CORS (staat open voor jouw Flutter web build)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # beperk desnoods tot je domeinen
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Config ----
API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "").strip()  # optioneel
CRON_TOKEN = os.getenv("CRON_TOKEN", "").strip()
SERVICE_TZ = timezone.utc

# football-data.org competitie-codes
COMP_CODES = {
    "Eredivisie": "DED",
    "Premier League": "PL",
    "La Liga": "PD",
    "Bundesliga": "BL1",
    "Ligue 1": "FL1",
}

# in-memory cache: { 'YYYY-MM-DD': [prediction, ...] }
CACHE: Dict[str, List[Dict[str, Any]]] = {}

# -------- HULPFUNCTIES --------

def _today_str() -> str:
    return datetime.now(SERVICE_TZ).strftime("%Y-%m-%d")

def _seeded_rng(key: str) -> random.Random:
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()
    seed = int(h[:16], 16)
    return random.Random(seed)

def _pct(x: float) -> float:
    # clamp & 2 decimals as float (we keep raw, UI rondt)
    return max(0.0, min(1.0, round(x, 4)))

def _score_bundle(rng: random.Random, p_home: float, p_draw: float, p_away: float) -> List[Dict[str, Any]]:
    """
    Simpele, deterministische scoreverdeling gebaseerd op p_home/draw/away.
    Geeft 5 scores met 'p' (som ~ 0.7..0.8). UI toont absoluut percentage per score.
    """
    # Basislijst met wat gangbare uitslagen
    candidates = [
        ("2-1", "home"), ("1-1", "draw"), ("2-0", "home"),
        ("3-1", "home"), ("1-2", "away"), ("0-0", "draw"),
        ("0-1", "away"), ("3-2", "home"), ("2-2", "draw")
    ]
    out: List[Tuple[str, float]] = []
    for score, side in candidates:
        base = {
            "home": 0.24,
            "draw": 0.16,
            "away": 0.18
        }[side]
        weight = {
            "home": p_home,
            "draw": p_draw,
            "away": p_away
        }[side]
        p = base * (0.7 + 0.6 * rng.random()) * weight / max(0.001, (p_home + p_draw + p_away))
        out.append((score, p))

    # Sorteer op p, neem top 5
    out.sort(key=lambda x: x[1], reverse=True)
    top5 = out[:5]
    # normaliseer zodat het netjes oogt (niet noodzakelijk)
    s = sum(p for _, p in top5) or 1.0
    norm = [(sc, min(0.35, p / s * 0.75)) for sc, p in top5]  # begrens
    return [{"score": sc, "p": round(p, 4)} for sc, p in norm]

def _avg_last3(rng: random.Random) -> float:
    return round(1.2 + 2.0 * rng.random(), 1)  # 1.2 .. 3.2

def _to_prediction(league: str, date_iso: str, home: str, away: str) -> Dict[str, Any]:
    # maak deterministische RNG op basis van teams+datum
    key = f"{league}|{date_iso}|{home}|{away}"
    rng = _seeded_rng(key)

    # Simpele kansen (home iets voordeel)
    base_home = 0.40 + 0.10 * rng.random()
    base_away = 0.30 + 0.10 * rng.random()
    base_draw = 1.0 - base_home - base_away
    # clamp
    p_home = _pct(base_home)
    p_away = _pct(base_away)
    p_draw = _pct(base_draw)

    # BTTS & overs (grof)
    p_btts = _pct(0.48 + 0.20 * rng.random())
    p_o15 = _pct(0.65 + 0.20 * rng.random())
    p_o25 = _pct(0.47 + 0.18 * rng.random())
    p_o35 = _pct(0.25 + 0.20 * rng.random())

    # Laatste 3 gemiddeld
    avg_home_l3 = _avg_last3(rng)
    avg_away_l3 = _avg_last3(rng)

    top_scores = _score_bundle(rng, p_home, p_draw, p_away)

    return {
        "home_team": home,
        "away_team": away,
        "league": league,
        "date": date_iso,
        "p_home": p_home,
        "p_draw": p_draw,
        "p_away": p_away,
        "p_btts_yes": p_btts,
        "p_over_15": p_o15,
        "p_over_25": p_o25,
        "p_over_35": p_o35,
        "avg_goals_home_last3": avg_home_l3,
        "avg_goals_away_last3": avg_away_l3,
        "top_scores": top_scores,
    }

# ------------- FETCH VAN football-data.org (optioneel) -------------

async def _fetch_fixtures_from_api(target_date: str) -> List[Dict[str, Any]]:
    """
    Probeert met football-data.org (v4) alle wedstrijden van target_date
    voor de 5 competities op te halen. Vereist API key in env.
    Returnt lijst predictions (met heuristische kansen).
    """
    if not API_KEY:
        return []

    comps = ",".join(COMP_CODES.values())
    url = f"https://api.football-data.org/v4/matches"
    params = {
        "dateFrom": target_date,
        "dateTo": target_date,
        "competitions": comps,
        "status": "SCHEDULED,IN_PLAY,PAUSED,FINISHED"  # ruim nemen
    }
    headers = {"X-Auth-Token": API_KEY}

    preds: List[Dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, params=params, headers=headers)
        if r.status_code != 200:
            return []
        data = r.json()
        for m in data.get("matches", []):
            comp_code = m.get("competition", {}).get("code")
            league = next((name for name, code in COMP_CODES.items() if code == comp_code), "Unknown")
            utc_date = m.get("utcDate", target_date)
            # Neem datum als YYYY-MM-DD
            date_iso = utc_date[:10]
            home = m.get("homeTeam", {}).get("name", "Home")
            away = m.get("awayTeam", {}).get("name", "Away")
            preds.append(_to_prediction(league, date_iso, home, away))
    return preds

# ---------------- MOCK FIXTURES (fallback) ----------------

def _mock_fixtures_for_date(date_iso: str) -> List[Dict[str, Any]]:
    """
    Fallback lijst: 0..1 wedstrijd per competitie (deterministisch)
    zodat de app altijd iets kan tonen.
    """
    rng = _seeded_rng(f"fixtures|{date_iso}")
    teams = {
        "Eredivisie": [
            ("Ajax", "Feyenoord"), ("PSV", "AZ"), ("Twente", "Utrecht")
        ],
        "Premier League": [
            ("Arsenal", "Chelsea"), ("Liverpool", "Man City"), ("Spurs", "Newcastle")
        ],
        "La Liga": [
            ("Real Madrid", "Barcelona"), ("Atletico", "Sevilla"), ("Valencia", "Villarreal")
        ],
        "Bundesliga": [
            ("Bayern", "Dortmund"), ("Leipzig", "Leverkusen"), ("Gladbach", "Stuttgart")
        ],
        "Ligue 1": [
            ("PSG", "Monaco"), ("Lyon", "Marseille"), ("Lille", "Nice")
        ],
    }

    out: List[Dict[str, Any]] = []
    for league, pool in teams.items():
        if rng.random() < 0.6:  # ~60% kans dat er die dag iets is
            home, away = pool[int(rng.random() * len(pool))]
            out.append(_to_prediction(league, date_iso, home, away))
        # anders: geen match -> die competitie geeft die dag niks terug
    return out

# ----------------- CACHE & AGGREGATIE -----------------

async def _get_predictions_for_date(date_iso: str) -> List[Dict[str, Any]]:
    # Cache check
    if date_iso in CACHE:
        return CACHE[date_iso]

    # 1) Probeer echte fixtures (als API key is gezet)
    preds = await _fetch_fixtures_from_api(date_iso)

    # 2) Fallback naar mock als leeg
    if not preds:
        preds = _mock_fixtures_for_date(date_iso)

    # sorteer (optioneel): bijv. per league dan alfabetisch
    preds.sort(key=lambda x: (x["league"], x["home_team"], x["away_team"]))

    CACHE[date_iso] = preds
    return preds

async def _warm_cache(days: int = 3, start_date: str | None = None) -> None:
    """
    Prefetch vandaag + komende 'days' dagen in cache.
    """
    if start_date:
        cur = datetime.fromisoformat(start_date).replace(tzinfo=SERVICE_TZ)
    else:
        cur = datetime.now(SERVICE_TZ)
    for i in range(days + 1):
        d = (cur + timedelta(days=i)).strftime("%Y-%m-%d")
        await _get_predictions_for_date(d)

# --------------- ENDPOINTS ----------------

@app.get("/")
def root():
    return {
        "ok": True,
        "service": "FootyPredict Mock API",
        "utc": datetime.now(SERVICE_TZ).isoformat()
    }

@app.get("/predictions")
async def predictions(d: str | None = Query(None, description="YYYY-MM-DD")):
    """
    Haal voorspellingen voor een datum op (default: vandaag, UTC).
    Lege lijst betekent: er zijn voor die dag (in onze 5 competities) geen wedstrijden.
    """
    date_iso = d or _today_str()
    preds = await _get_predictions_for_date(date_iso)
    return preds

@app.post("/refresh")
async def refresh(
    x_cron_token: str | None = Header(None, convert_underscores=False),
    days: int = Query(3, ge=0, le=14, description="Warm cache voor vandaag + n dagen"),
):
    """
    Warm de cache op (voor cron). Beschermd met X-CRON-TOKEN header.
    """
    if not CRON_TOKEN:
        raise HTTPException(status_code=400, detail="CRON_TOKEN not configured on server")
    if x_cron_token != CRON_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
    await _warm_cache(days=days)
    return {"ok": True, "warmed_days": days}

# --------------- STARTUP: kleine prewarm ---------------

@app.on_event("startup")
async def on_startup():
    # warm today + 1 dag zodat eerste request sneller is
    await _warm_cache(days=1)

# --------------- LOCAL DEV ----------------

if __name__ == "__main__":
    # Voor lokaal testen:
    #   python mock_api.py
    # en open: http://127.0.0.1:8888/predictions
    PORT = int(os.getenv("PORT", "8888"))
    print(f"Mock API running at http://127.0.0.1:{PORT}/predictions")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
