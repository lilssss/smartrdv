"""
Backend FastAPI — SmartRDV
==========================
Lance : python -m uvicorn main:app --reload --port 8000
"""
from __future__ import annotations
import json, os, subprocess, threading, sys
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

# ── Charge le .env si présent ────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from scheduler_optimizer import Weights, ScoringEngine, Optimizer
from scraped_loader import load_scraped_data
from nlp_engine import NLPEngine
from database import init_db, get_db, save_crawl_to_db, load_from_db, save_booking, get_booking_history, SessionLocal

_nlp = NLPEngine()

app = FastAPI(title="SmartRDV API", version="4.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def startup():
    init_db()
    print("[SmartRDV] Base de données prête ✅")

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
def get_practitioners(db: Session = Depends(get_db)):
    db_pracs = db.query(__import__('database').Practitioner).all()
    if db_pracs:
        return [{"id":p.id,"name":p.name,"specialty":p.specialty,"address":p.address,
                 "city":p.city,"profile_url":p.profile_url,"agenda_ids":p.agenda_ids or [],
                 "practice_ids":p.practice_ids or [],"visit_motive_ids":p.visit_motive_ids or []} for p in db_pracs]
    if not os.path.exists("slots.json"): return []
    with open("slots.json", encoding="utf-8") as f: data = json.load(f)
    return data.get("practitioners", [])

# ── Recommend ─────────────────────────────────────────────────

@app.post("/recommend", response_model=RecommendResponse)
def recommend(req: RecommendRequest, db: Session = Depends(get_db)):
    weights   = build_weights(req.preferences)
    engine    = ScoringEngine(weights)
    optimizer = Optimizer(engine)

    data_source   = "mock"
    practitioners = []
    raw_slots     = []

    # Priorité 1 : base de données
    try:
        prac_data, slot_data = load_from_db(db, req.specialty, req.location)
        if prac_data and slot_data:
            from doctolib_client import Practitioner as P, RawSlot as R
            practitioners = [P(id=p["id"],name=p["name"],specialty=p["specialty"],
                address=p["address"],city=p["city"],agenda_ids=p["agenda_ids"],
                practice_ids=p["practice_ids"],visit_motive_ids=p["visit_motive_ids"],
                profile_url=p["profile_url"]) for p in prac_data]
            prac_map_local = {p.id: p for p in practitioners}
            for s in slot_data:
                prac = prac_map_local.get(s["practitioner_id"])
                if prac:
                    raw_slots.append(R(start_date=s["start_date"],end_date=s["end_date"],
                        practitioner=prac,agenda_id=s["agenda_id"],visit_motive_id=s["visit_motive_id"]))
            if raw_slots: data_source = "db"
    except Exception as e:
        print(f"[API] Erreur DB : {e}")

    # Priorité 2 : slots.json (fallback)
    if not raw_slots and os.path.exists("slots.json"):
        try:
            with open("slots.json", encoding="utf-8") as f: meta = json.load(f)
            crawled = meta.get("specialty","").lower().replace("-","")
            asked   = req.specialty.lower().replace("-","")
            if crawled and (crawled in asked or asked in crawled):
                practitioners, raw_slots = load_scraped_data("slots.json")
                if raw_slots: data_source = "crawler"
        except Exception as e:
            print(f"[API] Erreur slots.json : {e}")

    # Priorité 3 : mock
    if not raw_slots:
        from doctolib_client import DoctolibSession, DoctolibSearcher, DoctolibSlotFetcher, UserPreferences
        session = DoctolibSession(); session.init()
        practitioners = DoctolibSearcher(session).search(req.specialty, req.location)
        for p in practitioners[:3]:
            raw_slots.extend(DoctolibSlotFetcher(session).fetch(p))
        data_source = "mock"

    if not raw_slots:
        raise HTTPException(status_code=404, detail="Aucun créneau disponible.")

    from doctolib_client import DoctolibAdapter, UserPreferences
    print(f"[Recommend] source={data_source} preferred_day={req.preferences.preferred_day}")
    up = UserPreferences(
        preferred_hours=(req.preferences.preferred_hours_start, req.preferences.preferred_hours_end),
        preferred_day=req.preferences.preferred_day,
        busy_days=req.preferences.busy_days,
    )
    scoring_slots = DoctolibAdapter(up).convert(raw_slots)
    ranked        = optimizer.rank(scoring_slots)
    top           = ranked[:req.top_n]

    day_names = {0:"lundi",1:"mardi",2:"mercredi",3:"jeudi",4:"vendredi",5:"samedi",6:"dimanche"}
    warning = None
    if req.preferences.preferred_day is not None:
        good_count = sum(1 for s in scoring_slots if s.conflict == 0.0)
        if good_count == 0:
            day_label = day_names.get(req.preferences.preferred_day, "ce jour")
            warning = f"Aucun créneau disponible {day_label} dans les données actuelles. Voici les meilleures alternatives."

    prac_map = {}
    for p in practitioners:
        if hasattr(p,'name'): prac_map[p.name] = p.profile_url
        elif isinstance(p,dict): prac_map[p.get('name','')] = p.get('profile_url','')

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
    from datetime import datetime as _dt

    # Date du jour — permet à l'IA de calculer "demain", "vendredi prochain", etc.
    now = _dt.now()
    JOURS = ["lundi","mardi","mercredi","jeudi","vendredi","samedi","dimanche"]
    today_name         = JOURS[now.weekday()]
    tomorrow_num       = (now.weekday() + 1) % 7
    tomorrow_name      = JOURS[tomorrow_num]
    after_tomorrow_num = (now.weekday() + 2) % 7

    SYSTEM = f"""Tu es SmartRDV, assistant médical IA.
Aujourd'hui c'est {today_name} {now.strftime('%d/%m/%Y')}. Demain c'est {tomorrow_name} (num={tomorrow_num}).
Réponds UNIQUEMENT en JSON valide, sans markdown, sans explication :
{{"intent":"book","specialty":"dermatologue"|"gynecologue"|"cardiologue"|"medecin-generaliste"|"ophtalmologue"|"psychiatre"|"dentiste"|"kinesitherapeute","location":"Paris","preferred_hours":[start,end],"preferred_day_num":0|1|2|3|4|5|6|null,"prefer_weekend":false,"is_new_patient":true|false|null,"motive":null,"is_teleconsult":false|null,"prefer_close":false,"message":"réponse courte en français"}}
Règles :
- intent="book" si l'utilisateur veut prendre un rendez-vous médical, sinon "chat"
- specialty : déduis la spécialité (gyné/gynéco → gynecologue, dermato → dermatologue, etc.)
- preferred_day_num : lundi=0,mardi=1,mercredi=2,jeudi=3,vendredi=4,samedi=5,dimanche=6
  "demain" → {tomorrow_num}, "après-demain" → {after_tomorrow_num}, null si non précisé
- preferred_hours : matin=[8,12], après-midi=[13,18], soir=[17,20], après le travail=[18,20] — null si non précisé
- message : résume en une phrase ce que tu as compris"""

    # ── Claude ────────────────────────────────────────────────
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY","")
    if anthropic_key.startswith("sk-ant"):
        try:
            from anthropic import Anthropic
            msgs = req.history + [{"role":"user","content":req.message}]
            resp = Anthropic(api_key=anthropic_key).messages.create(
                model="claude-sonnet-4-20250514", max_tokens=500, system=SYSTEM, messages=msgs)
            text = resp.content[0].text.strip().replace("```json","").replace("```","").strip()
            parsed = json.loads(text)
            print(f"[Claude] day={parsed.get('preferred_day_num')} spec={parsed.get('specialty')}")
            return parsed
        except Exception as e:
            print(f"[Chat] Claude erreur : {e}")

    # ── Gemini ────────────────────────────────────────────────
    google_key = os.environ.get("GOOGLE_API_KEY","")
    if google_key.startswith("AIza"):
        import requests as _req
        prompt = SYSTEM + f"\n\nMessage utilisateur : {req.message}"
        for model in ["gemini-2.5-flash","gemini-2.5-pro","gemini-2.0-flash-lite","gemini-2.0-flash"]:
            try:
                r = _req.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={google_key}",
                    json={"contents":[{"parts":[{"text":prompt}]}]}, timeout=15)
                if r.status_code == 404: continue
                r.raise_for_status()
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                text = text.strip().replace("```json","").replace("```","").strip()
                parsed = json.loads(text)
                print(f"[Gemini/{model}] day={parsed.get('preferred_day_num')} spec={parsed.get('specialty')}")
                return parsed
            except json.JSONDecodeError as e:
                raise HTTPException(status_code=502, detail=f"JSON invalide : {e}")
            except Exception as e:
                print(f"[Gemini/{model}] {e}"); continue
        raise HTTPException(status_code=502, detail="Gemini inaccessible")

    # ── NLP local ─────────────────────────────────────────────
    result = _nlp.analyze(req.message)
    print(f"[NLP local] day_num={getattr(result,'preferred_day_num',None)} spec={result.specialty}")
    return {
        "intent":            result.intent,
        "specialty":         result.specialty,
        "location":          result.location,
        "preferred_hours":   result.preferred_hours,
        "preferred_day_num": getattr(result,'preferred_day_num',None),
        "prefer_weekend":    result.prefer_weekend,
        "prefer_close":      getattr(result,'prefer_close',False),
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
        subprocess.run([sys.executable,"doctolib_crawler.py",req.specialty,req.location], capture_output=False)
    threading.Thread(target=run, daemon=True).start()
    return {"status":"started","specialty":req.specialty,"location":req.location}

@app.post("/crawl/auto")
def auto_crawl(req: CrawlRequest, db: Session = Depends(get_db)):
    subprocess.run([sys.executable,"doctolib_crawler.py",req.specialty,req.location], capture_output=False)
    if os.path.exists("slots.json"):
        try:
            with open("slots.json") as f:
                data = json.load(f)
            save_crawl_to_db(
                db,
                practitioners_data = data.get("practitioners", []),
                slots_data         = data.get("slots", []),
                specialty          = req.specialty,
                city               = req.location,
            )
            slots_count = len(data.get("slots",[]))
            pracs_count = len(data.get("practitioners",[]))
            return {"status":"success","specialty":req.specialty,"location":req.location,
                    "slots_count":slots_count,"practitioners":pracs_count,
                    "message":f"✅ {slots_count} créneaux sauvegardés en base"}
        except Exception as e:
            print(f"[Crawl] Erreur sauvegarde DB : {e}")
    return {"status":"error","message":"slots.json introuvable"}

# ── Book ──────────────────────────────────────────────────────

_book_state: dict = {"status": "idle", "result": None}

@app.post("/book")
def book_appointment(req: BookRequest):
    global _book_state
    if _book_state["status"] == "running":
        return {"status": "running", "message": "Booking déjà en cours..."}

    _book_state = {"status": "running", "result": None}

    profile_url    = req.profile_url
    slot_datetime  = req.slot_datetime
    is_new_patient = req.is_new_patient
    motive_keyword = req.motive_keyword
    is_teleconsult = req.is_teleconsult
    cwd            = os.getcwd()

    def run_booking():
        global _book_state
        print("[Book] run_booking DÉMARRÉ")
        try:
            script = f"""
import sys
sys.path.insert(0, r'{cwd}')
from doctolib_booking import DoctolibBooker
booker = DoctolibBooker(headless=False)
result = booker.book(
    profile_url={repr(profile_url)},
    slot_datetime={repr(slot_datetime)},
    is_new_patient={repr(is_new_patient)},
    motive_keyword={repr(motive_keyword)},
    is_teleconsult={repr(is_teleconsult)},
)
print("RESULT:" + str(result))
"""
            import tempfile
            tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8')
            tmp.write(script)
            tmp.close()
            print(f"[Book] Lancement : {sys.executable} {tmp.name}")
            r = subprocess.run([sys.executable, tmp.name], capture_output=True, text=True, timeout=180)
            os.unlink(tmp.name)
            output = r.stdout + r.stderr
            print(f"[Book] Output: {output[:300]}")
            if "RESULT:" in output:
                import ast
                result_str = output.split("RESULT:")[-1].strip().split("\n")[0]
                _book_state = {"status": "done", "result": ast.literal_eval(result_str)}
            else:
                _book_state = {"status": "error", "result": {"status": "error", "message": output[-300:] or "Erreur inconnue"}}
        except Exception as e:
            _book_state = {"status": "error", "result": {"status": "error", "message": str(e)}}
            print(f"[Book] Erreur : {e}")

    threading.Thread(target=run_booking, daemon=True).start()
    return {"status": "running", "message": "Navigateur ouvert — réservation en cours..."}

@app.get("/book/status")
def book_status():
    return {"booking_state": _book_state["status"], "result": _book_state.get("result")}

# ── Auth ──────────────────────────────────────────────────────

_login_state: dict = {"status": "idle", "error": ""}

@app.post("/auth/login")
def auth_login(req: LoginRequest):
    global _login_state
    if _login_state["status"] == "running":
        return {"status": "running", "message": "Navigateur déjà ouvert"}

    _login_state = {"status": "running", "error": ""}

    email    = req.email
    password = req.password
    cwd      = os.getcwd()

    def run_login():
        global _login_state
        print("[Auth] run_login DÉMARRÉ")
        try:
            script = f"""
import sys
sys.path.insert(0, r'{cwd}')
from doctolib_auth import login
try:
    cookies = login(email={repr(email)}, password={repr(password)}, headless=False)
    print("SUCCESS:" + str(len(cookies)))
except Exception as e:
    print("ERROR:" + str(e))
"""
            import tempfile
            tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8')
            tmp.write(script)
            tmp.close()
            print(f"[Auth] Lancement : {sys.executable} {tmp.name}")
            r = subprocess.run([sys.executable, tmp.name], capture_output=True, text=True, timeout=120)
            os.unlink(tmp.name)
            output = r.stdout + r.stderr
            print(f"[Auth] Output: {output[:300]}")
            if "SUCCESS:" in output:
                _login_state = {"status": "done", "error": ""}
            else:
                error = output.split("ERROR:")[-1].strip() if "ERROR:" in output else output[-200:]
                _login_state = {"status": "error", "error": error}
        except Exception as e:
            _login_state = {"status": "error", "error": str(e)}
            print(f"[Auth] Erreur : {e}")

    threading.Thread(target=run_login, daemon=True).start()
    return {"status": "running", "message": "Navigateur ouvert — connecte-toi"}

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
    return {"status":"success","message":"Session supprimée"}

# ── Historique & Stats DB ─────────────────────────────────────

@app.get("/history")
def booking_history(email: str = None, db: Session = Depends(get_db)):
    bookings = get_booking_history(db, user_email=email)
    return [{"id":b.id,"profile_url":b.profile_url,"slot_datetime":b.slot_datetime,
             "user_email":b.user_email,"status":b.status,"message":b.message,
             "created_at":b.created_at.isoformat() if b.created_at else ""} for b in bookings]

@app.get("/db/stats")
def db_stats(db: Session = Depends(get_db)):
    from database import Practitioner as P, Slot as S, Booking as B
    return {
        "practitioners": db.query(P).count(),
        "slots":         db.query(S).count(),
        "bookings":      db.query(B).count(),
        "db_file":       "smartrdv.db",
    }
