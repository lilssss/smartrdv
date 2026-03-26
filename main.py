"""
Backend FastAPI — SmartRDV
==========================
Lance : python -m uvicorn main:app --reload --port 8000
"""
from __future__ import annotations
import json, os, subprocess
from typing import Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── Charge le .env si présent (clé Anthropic, etc.) ─────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv non installé → variables d'env normales

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

class ApiKeyRequest(BaseModel):
    api_key: str

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
            "has_data":    True,
            "scraped_at":  data.get("scraped_at"),
            "specialty":   data.get("specialty"),
            "location":    data.get("location"),
            "practitioners": len(data.get("practitioners", [])),
            "slots":       len(data.get("slots", [])),
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

    # Priorité 1 : crawler si spécialité correspond
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

    # Priorité 2 : mock
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
    travel_w = req.preferences.weight_travel
    if req.preferences.prefer_close:
        travel_w = min(0.35, travel_w + 0.15)

    print(f"[Recommend] preferred_day={req.preferences.preferred_day} hours=({req.preferences.preferred_hours_start},{req.preferences.preferred_hours_end})")
    up = UserPreferences(
        preferred_hours=(req.preferences.preferred_hours_start, req.preferences.preferred_hours_end),
        preferred_day=req.preferences.preferred_day,
        busy_days=req.preferences.busy_days,
    )
    adapter       = DoctolibAdapter(up)
    scoring_slots = adapter.convert(raw_slots)
    ranked        = optimizer.rank(scoring_slots)

    # Si un jour précis est demandé, vérifie qu'il existe dans les résultats
    day_names = {0:"lundi",1:"mardi",2:"mercredi",3:"jeudi",4:"vendredi",5:"samedi",6:"dimanche"}
    if req.preferences.preferred_day is not None:
        good_count = sum(1 for s in scoring_slots if s.conflict == 0.0)
        if good_count == 0:
            print(f"[Recommend] Aucun créneau {day_names.get(req.preferences.preferred_day,'?')} — affiche les meilleurs disponibles")

    top = ranked[:req.top_n]

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
    # ── Clé Anthropic Claude ──────────────────────────────────
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if anthropic_key and anthropic_key.startswith("sk-ant"):
        try:
            from anthropic import Anthropic
            client = Anthropic(api_key=anthropic_key)
            SYSTEM = """Tu es SmartRDV, assistant médical IA.
Réponds UNIQUEMENT en JSON valide, sans markdown, sans explication :
{"intent":"book"|"chat","specialty":"dermatologue"|"gynecologue"|"cardiologue"|"medecin-generaliste"|"ophtalmologue"|"psychiatre"|"dentiste"|"kinesitherapeute","location":"Paris","preferred_hours":[start,end],"preferred_day_num":0|1|2|3|4|5|6|null,"prefer_weekend":false,"is_new_patient":true|false|null,"motive":null,"is_teleconsult":false|null,"prefer_close":false,"message":"réponse courte en français"}
Règles :
- intent="book" si l'utilisateur veut prendre un rendez-vous médical
- specialty : déduis la spécialité médicale la plus précise
- preferred_day_num : lundi=0, mardi=1, mercredi=2, jeudi=3, vendredi=4, samedi=5, dimanche=6 — null si non précisé
- preferred_hours : matin=[8,12], après-midi=[13,18], soir=[17,20], après le travail=[18,20], matin tôt=[7,9] — null si non précisé
- message : résume ce que tu as compris en une phrase naturelle"""
            msgs = req.history + [{"role":"user","content":req.message}]
            resp  = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=500, system=SYSTEM, messages=msgs)
            text = resp.content[0].text.strip().replace("```json","").replace("```","").strip()
            parsed = json.loads(text)
            print(f"[Claude] '{req.message}' → day={parsed.get('preferred_day_num')} hours={parsed.get('preferred_hours')} spec={parsed.get('specialty')}")
            return parsed
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=502, detail=f"Réponse Claude invalide : {e}")
        except Exception as e:
            print(f"[Chat] Erreur Claude API : {e}")
            raise HTTPException(status_code=502, detail=f"Erreur Claude API : {str(e)}")

    # ── Clé Google Gemini ─────────────────────────────────────
    google_key = os.environ.get("GOOGLE_API_KEY", "")
    if google_key and google_key.startswith("AIza"):
        PROMPT = """Tu es SmartRDV, assistant médical IA.
Réponds UNIQUEMENT en JSON valide, sans markdown, sans explication :
{"intent":"book","specialty":"dermatologue"|"gynecologue"|"cardiologue"|"medecin-generaliste"|"ophtalmologue"|"psychiatre"|"dentiste"|"kinesitherapeute","location":"Paris","preferred_hours":[start,end],"preferred_day_num":0|1|2|3|4|5|6|null,"prefer_weekend":false,"is_new_patient":true|false|null,"motive":null,"is_teleconsult":false|null,"prefer_close":false,"message":"réponse courte en français"}
Règles :
- intent="book" si l'utilisateur veut prendre un rendez-vous médical, sinon "chat"
- specialty : déduis la spécialité médicale (gyné/gynéco/gynécologique → gynecologue, dermato/dermatologue → dermatologue, etc.)
- preferred_day_num : lundi=0,mardi=1,mercredi=2,jeudi=3,vendredi=4,samedi=5,dimanche=6 — null si non précisé
- preferred_hours : matin=[8,12], après-midi=[13,18], soir=[17,20], après le travail=[18,20] — null si non précisé
- message : résume en une phrase ce que tu as compris

Message utilisateur : """ + req.message

        # Essaie plusieurs modèles Gemini dans l'ordre
        gemini_models = [
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-2.0-flash-lite",
            "gemini-2.0-flash",
        ]
        last_error = None
        for model in gemini_models:
            try:
                import requests as _req
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={google_key}"
                payload = {"contents": [{"parts": [{"text": PROMPT}]}]}
                r = _req.post(url, json=payload, timeout=15)
                if r.status_code == 404:
                    continue  # modèle pas disponible → essaie le suivant
                r.raise_for_status()
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                text = text.strip().replace("```json","").replace("```","").strip()
                parsed = json.loads(text)
                print(f"[Gemini/{model}] '{req.message}' → day={parsed.get('preferred_day_num')} hours={parsed.get('preferred_hours')} spec={parsed.get('specialty')}")
                return parsed
            except json.JSONDecodeError as e:
                raise HTTPException(status_code=502, detail=f"Réponse Gemini invalide (JSON) : {e}")
            except Exception as e:
                last_error = str(e)
                print(f"[Gemini/{model}] erreur : {e}")
                continue

        raise HTTPException(status_code=502, detail=f"Gemini inaccessible : {last_error}")

    # ── Pas de clé → NLP local ────────────────────────────────
    result = _nlp.analyze(req.message)
    print(f"[NLP local] '{req.message}' → day_num={result.preferred_day_num} hours={result.preferred_hours} spec={result.specialty}")
    return {
        "intent":           result.intent,
        "specialty":        result.specialty,
        "location":         result.location,
        "preferred_hours":  result.preferred_hours,
        "preferred_day_num":getattr(result, 'preferred_day_num', None),
        "prefer_weekend":   result.prefer_weekend,
        "prefer_close":     getattr(result, 'prefer_close', False),
        "is_new_patient":   result.is_new_patient,
        "motive":           result.motive,
        "is_teleconsult":   result.is_teleconsult,
        "message":          result.message,
        "nlp_source":       "local_rules",
        "confidence":       result.confidence,
        "detected":         result.detected,
    }

# ── Crawl ─────────────────────────────────────────────────────

@app.post("/crawl")
def launch_crawl(req: CrawlRequest):
    import threading
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

@app.post("/book")
def book_appointment(req: BookRequest):
    from doctolib_booking import DoctolibBooker
    booker = DoctolibBooker(headless=False)
    result = booker.book(
        profile_url=req.profile_url, slot_datetime=req.slot_datetime,
        is_new_patient=req.is_new_patient, motive_keyword=req.motive_keyword,
        is_teleconsult=req.is_teleconsult,
    )
    return result

# ── Auth ──────────────────────────────────────────────────────

@app.post("/auth/login")
def auth_login(req: LoginRequest):
    from doctolib_auth import login
    try:
        cookies = login(email=req.email, password=req.password, headless=False)
        return {"status":"success","message":f"Connecté — {len(cookies)} cookies","email":req.email}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/auth/status")
def auth_status():
    from doctolib_auth import session_info
    return session_info()

@app.post("/auth/logout")
def auth_logout():
    from doctolib_auth import logout
    logout()
    return {"status":"success","message":"Session supprimée"}

# ── Config API key ────────────────────────────────────────────

@app.post("/config/apikey")
def set_api_key(req: ApiKeyRequest):
    key = req.api_key.strip()
    env_path = ".env"

    if key.startswith("sk-ant"):
        os.environ["ANTHROPIC_API_KEY"] = key
        env_var = "ANTHROPIC_API_KEY"
        label = "Anthropic Claude"
    elif key.startswith("AIza"):
        os.environ["GOOGLE_API_KEY"] = key
        env_var = "GOOGLE_API_KEY"
        label = "Google Gemini"
    else:
        raise HTTPException(status_code=400, detail="Clé non reconnue — doit commencer par sk-ant (Anthropic) ou AIza (Google)")

    # Persiste dans .env
    lines = []
    if os.path.exists(env_path):
        with open(env_path) as f:
            lines = [l for l in f.readlines() if not l.startswith(env_var)]
    lines.append(f"{env_var}={key}\n")
    with open(env_path, "w") as f:
        f.writelines(lines)

    masked = key[:10] + "..." + key[-4:]
    return {"status": "success", "message": f"Clé {label} enregistrée ✅", "masked": masked, "provider": label}

@app.get("/config/apikey")
def get_api_key_status():
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    google_key    = os.environ.get("GOOGLE_API_KEY", "")
    if anthropic_key.startswith("sk-ant"):
        return {"configured": True, "masked": anthropic_key[:10] + "..." + anthropic_key[-4:], "provider": "Claude"}
    if google_key.startswith("AIza"):
        return {"configured": True, "masked": google_key[:10] + "..." + google_key[-4:], "provider": "Gemini"}
    return {"configured": False}