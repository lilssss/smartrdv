"""
Backend FastAPI — SmartRDV
==========================
Lance : python -m uvicorn main:app --reload --port 8000
"""
from __future__ import annotations
import json, os, subprocess, threading
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from scheduler_optimizer import Weights, ScoringEngine, Optimizer
from scraped_loader import load_scraped_data
from nlp_engine import NLPEngine

_nlp = NLPEngine()

app = FastAPI(title="SmartRDV API", version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Schémas ──────────────────────────────────────────────────

class PrefsInput(BaseModel):
    preferred_hours_start: int          = 9
    preferred_hours_end:   int          = 12
    preferred_day:         Optional[int]= None
    prefer_weekend:        bool         = False
    prefer_close:          bool         = False
    busy_days:             list[str]    = []
    weight_conflict:       float        = 0.35
    weight_interruption:   float        = 0.20
    weight_preference:     float        = 0.25
    weight_travel:         float        = 0.12
    weight_fatigue:        float        = 0.08

class RecommendRequest(BaseModel):
    specialty:   str        = "dermatologue"
    location:    str        = "Paris"
    top_n:       int        = 8
    preferences: PrefsInput = PrefsInput()

class SlotResult(BaseModel):
    rank:           int
    label:          str
    total_score:    float
    conflict:       float
    interruption:   float
    preference:     float
    travel:         float
    fatigue:        float
    is_recommended: bool

class RecommendResponse(BaseModel):
    best:                 SlotResult
    ranked:               list[SlotResult]
    total_slots_analyzed: int
    practitioners_found:  int
    weights_used:         dict
    data_source:          str
    practitioners_map:    dict = {}

class CrawlRequest(BaseModel):
    specialty: str = "dermatologue"
    location:  str = "Paris"

class ChatRequest(BaseModel):
    message: str
    history: list = []

class BookRequest(BaseModel):
    profile_url:    str
    slot_datetime:  str
    is_new_patient: bool         = True
    motive_keyword: Optional[str]= None
    is_teleconsult: bool         = False

class LoginRequest(BaseModel):
    email:    str
    password: str

# ── Helpers ───────────────────────────────────────────────────

def build_weights(p: PrefsInput) -> Weights:
    return Weights(
        conflict=p.weight_conflict,
        interruption=p.weight_interruption,
        preference=p.weight_preference,
        travel=p.weight_travel,
        fatigue=p.weight_fatigue,
    )

def to_slot_result(i: int, detail) -> SlotResult:
    c = detail.contributions
    return SlotResult(
        rank=i+1, label=detail.slot.time,
        total_score=round(detail.total, 4),
        conflict=round(c.get("conflict",0), 4),
        interruption=round(c.get("interruption",0), 4),
        preference=round(c.get("preference",0), 4),
        travel=round(c.get("travel",0), 4),
        fatigue=round(c.get("fatigue",0), 4),
        is_recommended=(i==0),
    )

# ── Health ────────────────────────────────────────────────────

@app.get("/")
def health():
    has_slots = os.path.exists("slots.json")
    slots_count = 0
    if has_slots:
        try:
            with open("slots.json") as f:
                data = json.load(f)
            slots_count = len(data.get("slots", []))
        except:
            pass
    return {"status": "ok", "slots_json": has_slots, "slots_count": slots_count}

@app.get("/status")
def status():
    if not os.path.exists("slots.json"):
        return {"has_data": False, "message": "Pas de slots.json"}
    try:
        with open("slots.json") as f:
            data = json.load(f)
        return {
            "has_data":      True,
            "scraped_at":    data.get("scraped_at"),
            "specialty":     data.get("specialty"),
            "location":      data.get("location"),
            "practitioners": len(data.get("practitioners", [])),
            "slots":         len(data.get("slots", [])),
        }
    except Exception as e:
        return {"has_data": False, "error": str(e)}

@app.get("/practitioners")
def get_practitioners():
    if not os.path.exists("slots.json"):
        return []
    with open("slots.json", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("practitioners", [])

# ── Recommend ─────────────────────────────────────────────────

@app.post("/recommend", response_model=RecommendResponse)
def recommend(req: RecommendRequest):
    weights   = build_weights(req.preferences)
    engine    = ScoringEngine(weights)
    optimizer = Optimizer(engine)

    data_source   = "mock"
    practitioners = []
    raw_slots     = []

    if os.path.exists("slots.json"):
        try:
            with open("slots.json", encoding="utf-8") as f:
                meta = json.load(f)
            crawled = meta.get("specialty","").lower().replace("-","")
            asked   = req.specialty.lower().replace("-","")
            if crawled and (crawled in asked or asked in crawled):
                practitioners, raw_slots = load_scraped_data("slots.json")
                if raw_slots:
                    data_source = "crawler"
        except Exception as e:
            print(f"[API] Erreur slots.json : {e}")

    if not raw_slots:
        from doctolib_client import DoctolibSession, DoctolibSearcher, DoctolibSlotFetcher, UserPreferences
        up = UserPreferences(preferred_hours=(req.preferences.preferred_hours_start, req.preferences.preferred_hours_end))
        session = DoctolibSession(); session.init()
        searcher = DoctolibSearcher(session)
        fetcher  = DoctolibSlotFetcher(session)
        practitioners = searcher.search(req.specialty, req.location)
        for p in practitioners[:3]:
            raw_slots.extend(fetcher.fetch(p))
        data_source = "mock"

    if not raw_slots:
        raise HTTPException(status_code=404, detail="Aucun créneau disponible.")

    from doctolib_client import DoctolibAdapter, UserPreferences
    print(f"[Recommend] preferred_day={req.preferences.preferred_day} hours=({req.preferences.preferred_hours_start},{req.preferences.preferred_hours_end})")
    up = UserPreferences(
        preferred_hours=(req.preferences.preferred_hours_start, req.preferences.preferred_hours_end),
        preferred_day=req.preferences.preferred_day,
        busy_days=req.preferences.busy_days,
    )
    adapter       = DoctolibAdapter(up)
    scoring_slots = adapter.convert(raw_slots)
    ranked        = optimizer.rank(scoring_slots)
    top           = ranked[:req.top_n]

    prac_map = {}
    for p in practitioners:
        if hasattr(p, 'name') and hasattr(p, 'profile_url'):
            prac_map[p.name] = p.profile_url
        elif isinstance(p, dict):
            prac_map[p.get('name','')] = p.get('profile_url','')

    day_names = {0:"lundi",1:"mardi",2:"mercredi",3:"jeudi",4:"vendredi",5:"samedi",6:"dimanche"}
    warning = None
    if req.preferences.preferred_day is not None:
        good_count = sum(1 for s in scoring_slots if s.conflict == 0.0)
        if good_count == 0:
            day_label = day_names.get(req.preferences.preferred_day, "ce jour")
            warning = f"Aucun créneau disponible {day_label} dans les données actuelles. Voici les meilleures alternatives."

    return {
        "best":                  to_slot_result(0, ranked[0]).dict(),
        "ranked":                [to_slot_result(i, d).dict() for i, d in enumerate(top)],
        "total_slots_analyzed":  len(scoring_slots),
        "practitioners_found":   len(practitioners),
        "weights_used":          weights.as_dict(),
        "data_source":           data_source,
        "practitioners_map":     prac_map,
        "warning":               warning,
    }

# ── Chat / NLP ────────────────────────────────────────────────

@app.post("/chat")
def chat(req: ChatRequest):
    import os as _os

    # Claude
    api_key = _os.environ.get("ANTHROPIC_API_KEY","")
    if api_key and api_key.startswith("sk-ant"):
        try:
            from anthropic import Anthropic
            client = Anthropic(api_key=api_key)
            SYSTEM = """Tu es SmartRDV, assistant médical IA.
Réponds UNIQUEMENT en JSON :
{"intent":"book"|"chat","specialty":"dermatologue|gynecologue|cardiologue|medecin-generaliste|ophtalmologue|psychiatre|dentiste|kinesitherapeute","location":"Paris","preferred_hours":[start,end],"preferred_day_num":0-6|null,"prefer_weekend":false,"is_new_patient":true|false|null,"motive":null,"is_teleconsult":false|null,"prefer_close":false,"message":"réponse en français"}
Jours: lundi=0,mardi=1,mercredi=2,jeudi=3,vendredi=4,samedi=5,dimanche=6
Horaires: matin=[8,12],après-midi=[13,18],soir=[17,20],après le travail=[18,20]"""
            msgs = req.history + [{"role":"user","content":req.message}]
            resp = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=500, system=SYSTEM, messages=msgs)
            text = resp.content[0].text
            import json as _j
            return _j.loads(text.replace("```json","").replace("```","").strip())
        except Exception as e:
            print(f"[Chat] Claude erreur : {e}")

    # Gemini
    google_key = _os.environ.get("GOOGLE_API_KEY", "")
    if google_key and google_key.startswith("AIza"):
        PROMPT = """Tu es SmartRDV, assistant médical IA.
Réponds UNIQUEMENT en JSON valide, sans markdown, sans explication :
{"intent":"book"|"chat","specialty":"dermatologue|gynecologue|cardiologue|medecin-generaliste|ophtalmologue|psychiatre|dentiste|kinesitherapeute","location":"Paris","preferred_hours":[start,end],"preferred_day_num":0-6|null,"prefer_weekend":false,"is_new_patient":true|false|null,"motive":null,"is_teleconsult":false|null,"prefer_close":false,"message":"résumé en une phrase ce que tu as compris"}
Règles :
- intent="book" si l'utilisateur veut prendre un rendez-vous médical, sinon "chat"
- specialty : déduis la spécialité médicale
- preferred_day_num : lundi=0,mardi=1,mercredi=2,jeudi=3,vendredi=4,samedi=5,dimanche=6 — null si non précisé
- preferred_hours : matin=[8,12], après-midi=[13,18], soir=[17,20], après le travail=[18,20] — null si non précisé

Message utilisateur : """ + req.message

        for model in ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash-lite", "gemini-2.0-flash"]:
            try:
                import requests as _req
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={google_key}"
                r = _req.post(url, json={"contents": [{"parts": [{"text": PROMPT}]}]}, timeout=15)
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                import json as _j
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                parsed = _j.loads(text.replace("```json","").replace("```","").strip())
                print(f"[Gemini/{model}] '{req.message}' → day={parsed.get('preferred_day_num')} hours={parsed.get('preferred_hours')} spec={parsed.get('specialty')}")
                return parsed
            except Exception as e:
                print(f"[Gemini/{model}] erreur : {e}")
                continue

    # NLP local fallback
    result = _nlp.analyze(req.message)
    print(f"[NLP] '{req.message}' → day_num={getattr(result,'preferred_day_num',None)} hours={result.preferred_hours} spec={result.specialty}")
    return {
        "intent":            result.intent,
        "specialty":         result.specialty,
        "location":          result.location,
        "preferred_hours":   result.preferred_hours,
        "preferred_day_num": getattr(result, 'preferred_day_num', None),
        "prefer_weekend":    result.prefer_weekend,
        "prefer_close":      getattr(result, 'prefer_close', False),
        "is_new_patient":    result.is_new_patient,
        "motive":            result.motive,
        "is_teleconsult":    result.is_teleconsult,
        "message":           result.message,
        "nlp_source":        "local_rules",
        "confidence":        result.confidence,
        "detected":          result.detected,
    }

# ── Crawl ─────────────────────────────────────────────────────

@app.post("/crawl")
def launch_crawl(req: CrawlRequest):
    def run():
        subprocess.run(["python","doctolib_crawler.py",req.specialty,req.location], capture_output=False)
    threading.Thread(target=run, daemon=True).start()
    return {"status":"started","specialty":req.specialty,"location":req.location}

@app.post("/crawl/auto")
def auto_crawl(req: CrawlRequest):
    subprocess.run(["python","doctolib_crawler.py",req.specialty,req.location], capture_output=False)
    if os.path.exists("slots.json"):
        try:
            with open("slots.json") as f:
                data = json.load(f)
            return {"status":"success","specialty":req.specialty,"location":req.location,
                    "slots_count":len(data.get("slots",[])), "practitioners":len(data.get("practitioners",[])),
                    "message":f"✅ {len(data.get('slots',[]))} créneaux pour {req.specialty}"}
        except:
            pass
    return {"status":"error","message":"slots.json introuvable"}

# ── Book ──────────────────────────────────────────────────────

# État global du booking (thread séparé pour ne pas bloquer uvicorn)
_book_state: dict = {"status": "idle", "result": None}

@app.post("/book")
def book_appointment(req: BookRequest):
    global _book_state
    if _book_state["status"] == "running":
        return {"status": "running", "message": "Booking déjà en cours..."}

    _book_state = {"status": "running", "result": None}

    def run_booking():
        global _book_state
        try:
            from doctolib_booking import DoctolibBooker
            booker = DoctolibBooker(headless=False)
            result = booker.book(
                profile_url    = req.profile_url,
                slot_datetime  = req.slot_datetime,
                is_new_patient = req.is_new_patient,
                motive_keyword = req.motive_keyword,
                is_teleconsult = req.is_teleconsult,
            )
            _book_state = {"status": "done", "result": result}
            print(f"[Book] Terminé : {result}")
        except Exception as e:
            _book_state = {"status": "error", "result": {"status": "error", "message": str(e)}}
            print(f"[Book] Erreur : {e}")

    threading.Thread(target=run_booking, daemon=True).start()
    return {"status": "running", "message": "Navigateur ouvert — réservation en cours..."}

@app.get("/book/status")
def book_status():
    return {
        "booking_state": _book_state["status"],
        "result":        _book_state.get("result"),
    }

# ── Auth ──────────────────────────────────────────────────────

# État global du login (thread séparé pour ne pas bloquer uvicorn)
_login_state: dict = {"status": "idle", "error": ""}

@app.post("/auth/login")
def auth_login(req: LoginRequest):
    global _login_state
    if _login_state["status"] == "running":
        return {"status": "running", "message": "Navigateur déjà ouvert, connecte-toi dedans."}

    _login_state = {"status": "running", "error": ""}

    def run_login():
        global _login_state
        try:
            from doctolib_auth import login
            cookies = login(email=req.email, password=req.password)
            _login_state = {"status": "done", "error": ""}
            print(f"[Auth] Login terminé — {len(cookies)} cookies")
        except Exception as e:
            _login_state = {"status": "error", "error": str(e)}
            print(f"[Auth] Login erreur : {e}")

    threading.Thread(target=run_login, daemon=True).start()
    return {"status": "running", "message": "Navigateur ouvert — connecte-toi et saisis le code 2FA si demandé."}

@app.get("/auth/status")
def auth_status():
    from doctolib_auth import session_info
    info = session_info()
    info["login_state"] = _login_state["status"]
    info["login_error"] = _login_state.get("error", "")
    return info

@app.post("/auth/logout")
def auth_logout():
    global _login_state
    _login_state = {"status": "idle", "error": ""}
    from doctolib_auth import logout
    logout()
    return {"status": "success", "message": "Session supprimée"}

# ── Config API Key ─────────────────────────────────────────────

@app.get("/config/apikey")
def get_apikey():
    key = os.environ.get("GOOGLE_API_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return {"configured": False}
    if key.startswith("AIza"):
        return {"configured": True, "provider": "gemini", "preview": key[:10] + "..." + key[-4:]}
    if key.startswith("sk-ant"):
        return {"configured": True, "provider": "claude", "preview": key[:10] + "..." + key[-4:]}
    return {"configured": True, "provider": "unknown", "preview": key[:6] + "..."}

@app.post("/config/apikey")
def set_apikey(body: dict):
    key = body.get("key", "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="Clé vide")
    env_path  = ".env"
    lines     = []
    if os.path.exists(env_path):
        with open(env_path) as f:
            lines = f.readlines()
    env_var   = "GOOGLE_API_KEY" if key.startswith("AIza") else "ANTHROPIC_API_KEY"
    written   = False
    new_lines = []
    for line in lines:
        if line.startswith("GOOGLE_API_KEY=") or line.startswith("ANTHROPIC_API_KEY="):
            new_lines.append(f"{env_var}={key}\n")
            written = True
        else:
            new_lines.append(line)
    if not written:
        new_lines.append(f"{env_var}={key}\n")
    with open(env_path, "w") as f:
        f.writelines(new_lines)
    os.environ[env_var] = key
    provider = "gemini" if key.startswith("AIza") else "claude"
    return {"status": "ok", "provider": provider, "preview": key[:10] + "..." + key[-4:]}