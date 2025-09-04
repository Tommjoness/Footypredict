import os
import json
import random
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Any, List, Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
import httpx

app = FastAPI(title="FootyPredict Mock API")

# === Config ===
DATA_FILE = Path("data.json")
CRON_TOKEN = os.getenv("CRON_TOKEN", "")
FD_API_KEY = os.getenv("FOOTBALL_DATA_API_KEY")  # optional

# football-data.org competition codes
COMP_CODES = {
    "Eredivisie": "DED",
    "Premier League": "PL",
    "La Liga": "PD",
    "Bundesliga": "BL1",
    "Ligue 1": "FL1",
}

# In-memory cache
CACHE: Dict[str, Any] = {
    # "2025-09-03": { "items": [...], "by_league": {...}, "generated_at": "..."}
}

# ---------- helpers ----------

def _today_str() -> str:
    return date.today().isoformat()

def load_cache_from_disk() -> None:
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                CACHE.clear()
                CACHE.update(data)
        except Exception:
            pass  # ignore corrupt file

def save_cache_to_disk() -> None:
    try:
        DATA_FILE.write_text(json.dumps(CACHE, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def one_dec(x: float) -> float:
    return round(float(x) + 1e-9, 1)

def pct(x: float) -> float:
    return round(100.0 * x)

def _demo_prediction(league: str, home: str, away: str, dt: str) -> Dict[str, Any]:
    """Fallback: maak nette demo-data met plausibele percentages."""
    # willekeurige probabilities die netjes optellen
    p_home = random.uniform(0.35, 0.55)
    p_draw = random.uniform(0.20, 0.30)
    p_away = max(0.0, 1.0 - p_home - p_draw)
    # btts / overs
    p_btts = random.uniform(0.45, 0.65)
    p_o15  = random.uniform(0.65, 0.80)
    p_o25  = random.uniform(0.45, 0.60)
    p_o35  = random.uniform(0.25, 0.40)

    # avg goals last 3 (fake maar stabiel)
    avg_home = one_dec(random.uniform(1.4, 2.3))
    avg_away = one_dec(random.uniform(1.0, 2.1))

    # top scores
    scores = [("2-1", 0.18), ("1-1", 0.16), ("2-0", 0.12), ("3-1", 0.11), ("1-2", 0.10)]
    top_scores = [{"score": s, "p": p} for s, p in scores]

    return {
        "home_team": home,
        "away_team": away,
        "league": league,
        "date": dt,
        "p_home": round(p_home, 2),
        "p_draw": round(p_draw, 2),
        "p_away": round(p_away, 2),
        "p_btts_yes": round(p_btts, 2),
        "p_over_15": round(p_o15, 2),
        "p_over_25": round(p_o25, 2),
        "p_over_35": round(p_o35, 2),
        "avg_goals_home_last3": avg_home,
        "avg_goals_away_last3": avg_away,
        "top_scores": top_scores,
    }

async def _fetch_from_football_data(target_date: str) -> Dict[str, Any]:
    """
    Haal fixtures per competitie op (zonder echte odds).
    Als geen API-key aanwezig is, bouw demo-data.
    Resultaat:
    {
      "items": [ ...flat list van predictions... ],
      "by_league": {
        "Eredivisie": {"matches":[...]} of {"message":"Geen wedstrijden"}
      },
      "generated_at": "...UTC..."
    }
    """
    generated_at = datetime.utcnow().isoformat()
    out_items: List[Dict[str, Any]] = []
    by_league: Dict[str, Any] = {}

    # Zonder key: maak demowedstrijden per competitie
    if not FD_API_KEY:
        rnd = random.Random(target_date)  # deterministisch per datum
        for league in COMP_CODES.keys():
            # 0–3 demo-wedstrijden
            n = rnd.randint(0, 3)
            if n == 0:
                by_league[league] = {"message": "Geen wedstrijden"}
                continue
            matches = []
            for i in range(n):
                home = f"{league.split()[0]} Team {i+1}"
                away = f"{league.split()[0]} Team {i+2}"
                pred = _demo_prediction(league, home, away, target_date)
                out_items.append(pred)
                matches.append(pred)
            by_league[league] = {"matches": matches}
        return {"items": out_items, "by_league": by_league, "generated_at": generated_at}

    # Met echte API-key: haal fixtures en bouw synthetische odds (demo)
    headers = {"X-Auth-Token": FD_API_KEY}
    async with httpx.AsyncClient(timeout=20) as client:
        for league, code in COMP_CODES.items():
            try:
                url = f"https://api.football-data.org/v4/competitions/{code}/matches"
                params = {"dateFrom": target_date, "dateTo": target_date}
                r = await client.get(url, headers=headers, params=params)
                r.raise_for_status()
                data = r.json()
                matches = data.get("matches", []) or []
                league_list = []
                if not matches:
                    by_league[league] = {"message": "Geen wedstrijden"}
                    continue
                for m in matches:
                    home = m["homeTeam"]["name"]
                    away = m["awayTeam"]["name"]
                    # synthetische odds (tot je echte model koppelt)
                    pred = _demo_prediction(league, home, away, target_date)
                    out_items.append(pred)
                    league_list.append(pred)
                by_league[league] = {"matches": league_list}
            except Exception:
                # bij fout: toon geen crashes, maar "geen wedstrijden"
                by_league[league] = {"message": "Geen wedstrijden"}

    return {"items": out_items, "by_league": by_league, "generated_at": generated_at}

async def build_or_get_for_date(target_date: str) -> Dict[str, Any]:
    # cache opvragen of maken
    if target_date in CACHE:
        return CACHE[target_date]
    data = await _fetch_from_football_data(target_date)
    CACHE[target_date] = data
    save_cache_to_disk()
    return data

# ---------- endpoints ----------

@app.get("/")
async def root():
    return {"ok": True, "service": "FootyPredict Mock API", "utc": datetime.utcnow().isoformat()}

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/predictions")
async def predictions(d: Optional[str] = Query(None, description="YYYY-MM-DD")):
    """
    Haal predictions voor een dag op (alle competities samengevoegd).
    Voor jouw Flutter-app kun je d weglaten; default = vandaag.
    """
    target = d or _today_str()
    data = await build_or_get_for_date(target)
    # Voor backward compatibility geven we nog steeds een platte lijst terug
    return JSONResponse(data["items"])

@app.post("/refresh")
async def refresh(x_cron_token: Optional[str] = Header(None, convert_underscores=False),
                  d: Optional[str] = Query(None, description="YYYY-MM-DD (optioneel)")):
    """
    Wordt dagelijks aangeroepen door GitHub Actions.
    Vereist header: X-CRON-TOKEN: <token>
    """
    if not CRON_TOKEN:
        raise HTTPException(status_code=500, detail="CRON_TOKEN is niet geconfigureerd")
    if x_cron_token is None or x_cron_token != CRON_TOKEN:
        raise HTTPException(status_code=401, detail="Ongeldige of ontbrekende X-CRON-TOKEN")

    target = d or _today_str()
    data = await _fetch_from_football_data(target)
    CACHE[target] = data
    save_cache_to_disk()
    return {"ok": True, "refreshed": target, "items": len(data.get("items", []))}

# ---------- startup ----------

@app.on_event("startup")
async def _startup():
    load_cache_from_disk()
    # zorg dat vandaag alvast gevuld is (handig bij cold start)
    try:
        await build_or_get_for_date(_today_str())
    except Exception:
        pass

# ---------- local run ----------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8888"))
    # host=0.0.0.0 is voor Render; lokaal kan je 127.0.0.1 gebruiken
    uvicorn.run("mock_api:app", host="0.0.0.0", port=port, reload=False)
