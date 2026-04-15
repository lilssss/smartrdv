"""
Backend FastAPI — SmartRDV
==========================
Lance : python -m uvicorn main:app --reload --port 8000
"""
from __future__ import annotations
import json, os, re, subprocess, threading, sys
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

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

try:
    from calendar_scheduler import CalendarScheduler
    _scheduler = CalendarScheduler()
    print("[SmartRDV] CalendarScheduler chargé ✅")
except Exception as e:
    _scheduler = None
    print(f"[SmartRDV] CalendarScheduler indisponible : {e}")

app = FastAPI(title="SmartRDV API", version="4.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def startup():
    init_db()
    print("[SmartRDV] Base de données prête ✅")

# ── Schémas ───────────────────────────────────────────────────

class PrefsInput(BaseModel):
    preferred_hours_start: int           = 9
    preferred_hours_end:   int           = 12
    preferred_day:         Optional[int] = None
    preferred_date:        Optional[str] = None  # "YYYY-MM-DD"
    prefer_weekend:        bool          = False
    prefer_close:          bool          = False
    busy_days:             list[str]     = []
    weight_conflict:       float         = 0.35
    weight_interruption:   float         = 0.20
    weight_preference:     float         = 0.25
    weight_travel:         float         = 0.12
    weight_fatigue:        float         = 0.08

class RecommendRequest(BaseModel):
    specialty:   str        = "dermatologue"
    location:    str        = "Paris"
    user_origin: str        = ""           # position GPS/adresse réelle pour Maps
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
    is_new_patient: bool          = True
    motive_keyword: Optional[str] = None
    is_teleconsult: bool          = False

class LoginRequest(BaseModel):
    email:    str
    password: str

# ── Helpers ───────────────────────────────────────────────────

def build_weights(p: PrefsInput) -> Weights:
    return Weights(
        conflict=p.weight_conflict, interruption=p.weight_interruption,
        preference=p.weight_preference, travel=p.weight_travel, fatigue=p.weight_fatigue,
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
            with open("slots.json") as f: data = json.load(f)
            slots_count = len(data.get("slots", []))
        except: pass
    return {"status": "ok", "slots_json": has_slots, "slots_count": slots_count}

@app.get("/status")
def status():
    if not os.path.exists("slots.json"):
        return {"has_data": False, "message": "Pas de slots.json"}
    try:
        with open("slots.json") as f: data = json.load(f)
        return {
            "has_data": True, "scraped_at": data.get("scraped_at"),
            "specialty": data.get("specialty"), "location": data.get("location"),
            "practitioners": len(data.get("practitioners", [])),
            "slots": len(data.get("slots", [])),
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

@app.post("/recommend")
def recommend(req: RecommendRequest, db: Session = Depends(get_db)):
    weights   = build_weights(req.preferences)
    engine    = ScoringEngine(weights)
    optimizer = Optimizer(engine)

    data_source   = "mock"
    practitioners = []
    raw_slots     = []

    # Priorité 1 : DB
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

    # Priorité 2 : slots.json
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
        from doctolib_client import DoctolibSession, DoctolibSearcher, DoctolibSlotFetcher
        session = DoctolibSession(); session.init()
        practitioners = DoctolibSearcher(session).search(req.specialty, req.location)
        for p in practitioners[:3]:
            raw_slots.extend(DoctolibSlotFetcher(session).fetch(p))
        data_source = "mock"

    if not raw_slots:
        raise HTTPException(status_code=404, detail="Aucun créneau disponible.")

    print(f"[Recommend] source={data_source} day={req.preferences.preferred_day} date={req.preferences.preferred_date} origin={req.user_origin or 'Paris'}")

    # ── Scoring Calendar + Maps ───────────────────────────────
    scoring_source = "mock"
    scoring_slots  = []
    raw_slots_enriched = []

    try:
        scheduler = _scheduler
        if not scheduler:
            raise Exception("CalendarScheduler non disponible")

        # Utilise la vraie position utilisateur pour Maps
        scheduler.user_location = req.user_origin if req.user_origin else req.location

        prac_map_by_id = {}
        for p in practitioners:
            pid = p.id if hasattr(p,'id') else (p.get('id',0) if isinstance(p,dict) else 0)
            prac_map_by_id[pid] = p

        for s in raw_slots:
            prac_id = s.practitioner.id if hasattr(s,'practitioner') and s.practitioner else 0
            prac    = prac_map_by_id.get(prac_id)
            address = ""
            name    = ""
            if prac:
                address = getattr(prac,'address','') or (prac.get('address','') if isinstance(prac,dict) else '')
                name    = getattr(prac,'name','')    or (prac.get('name','')    if isinstance(prac,dict) else '')
            raw_slots_enriched.append({
                "start_date": getattr(s,'start_date',''),
                "address":    address or req.location,
                "name":       name,
            })

        scoring_slots = scheduler.score_slots(
            raw_slots_enriched,
            preferred_hours = [req.preferences.preferred_hours_start, req.preferences.preferred_hours_end],
            preferred_day   = req.preferences.preferred_day,
            preferred_date  = req.preferences.preferred_date,
            location        = req.location,
        )
        scoring_source = "calendar+maps"
        print(f"[Recommend] Scoring Calendar+Maps : {len(scoring_slots)} slots")

    except Exception as e:
        print(f"[Recommend] Fallback scoring mock : {e}")
        from doctolib_client import DoctolibAdapter, UserPreferences
        up = UserPreferences(
            preferred_hours=(req.preferences.preferred_hours_start, req.preferences.preferred_hours_end),
            preferred_day=req.preferences.preferred_day,
            busy_days=req.preferences.busy_days,
        )
        scoring_slots  = DoctolibAdapter(up).convert(raw_slots)
        scoring_source = "mock"

    # ── Vérification finale ───────────────────────────────────
    if not scoring_slots:
        day_names = {0:"lundi",1:"mardi",2:"mercredi",3:"jeudi",4:"vendredi",5:"samedi",6:"dimanche"}
        if req.preferences.preferred_date:
            msg = f"Aucun créneau disponible le {req.preferences.preferred_date}."
        elif req.preferences.preferred_day is not None:
            day_label = day_names.get(req.preferences.preferred_day, "ce jour")
            msg = f"Aucun créneau disponible le {day_label}."
        else:
            msg = "Aucun créneau disponible."
        return {
            "no_slots": True,
            "message":  msg,
            "best": None, "ranked": [], "total_slots_analyzed": 0,
            "practitioners_found": len(practitioners),
            "weights_used": weights.as_dict(),
            "data_source": data_source, "scoring_source": scoring_source,
            "practitioners_map": {}, "warning": msg,
        }

    ranked = optimizer.rank(scoring_slots)
    top    = ranked[:req.top_n]

    prac_map = {}
    for p in practitioners:
        if hasattr(p,'name'): prac_map[p.name] = p.profile_url
        elif isinstance(p,dict): prac_map[p.get('name','')] = p.get('profile_url','')

    # Récupère les conflits Calendar pour les créneaux classés
    conflicts_map = {}
    if scoring_source == "calendar+maps" and _scheduler and hasattr(_scheduler, 'slot_conflicts'):
        for d in top:
            label = d.slot.time
            if label in _scheduler.slot_conflicts:
                conflicts_map[label] = _scheduler.slot_conflicts[label]

    return {
        "best":                  to_slot_result(0, ranked[0]).dict(),
        "ranked":                [to_slot_result(i, d).dict() for i, d in enumerate(top)],
        "total_slots_analyzed":  len(scoring_slots),
        "practitioners_found":   len(practitioners),
        "weights_used":          weights.as_dict(),
        "data_source":           data_source,
        "scoring_source":        scoring_source,
        "practitioners_map":     prac_map,
        "conflicts":             conflicts_map,
        "warning":               None,
    }

# ── Chat / NLP ────────────────────────────────────────────────

@app.post("/chat")
def chat(req: ChatRequest):
    now   = datetime.now()
    JOURS = ["lundi","mardi","mercredi","jeudi","vendredi","samedi","dimanche"]
    today_name          = JOURS[now.weekday()]
    tomorrow_date       = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    after_tomorrow_date = (now + timedelta(days=2)).strftime("%Y-%m-%d")

    SYSTEM = f"""Tu es kAIros, assistant IA de planning.
Aujourd'hui : {today_name} {now.strftime('%d/%m/%Y')}. Demain : {tomorrow_date}.
Réponds UNIQUEMENT en JSON valide, sans markdown :
{{"intent":"book"|"chat","platform":"doctolib"|"planity","specialty":"dermatologue"|"gynecologue"|"cardiologue"|"medecin-generaliste"|"ophtalmologue"|"psychiatre"|"dentiste"|"kinesitherapeute"|"coiffeur"|"barbier"|"spa"|"manucure"|"institut-de-beaute"|"massotherapeute"|"sophrologue"|"naturopathe","location":"Paris","preferred_hours":[start,end]|null,"preferred_day_num":0|1|2|3|4|5|6|null,"preferred_date":"YYYY-MM-DD"|null,"prefer_weekend":false,"is_new_patient":true|false|null,"motive":null,"is_teleconsult":false|null,"prefer_close":false,"message":"réponse courte en français"}}
Règles :
- intent="book" si réservation demandée (rdv médical, coiffeur, spa, etc.), sinon "chat"
- platform="doctolib" pour tout ce qui est médical (médecin, spécialiste, kiné...)
- platform="planity" pour tout ce qui est bien-être/beauté (coiffeur, barbier, spa, manucure, massage, sophrologue, naturopathe...)
- preferred_date : date précise → "YYYY-MM-DD" (ex: "17 avril"→"{now.year}-04-17", "demain"→"{tomorrow_date}", "après-demain"→"{after_tomorrow_date}", "aujourd'hui"/"ce matin"/"cet après-midi"/"ce soir"/"maintenant"→"{now.strftime('%Y-%m-%d')}")
- preferred_day_num : lundi=0,mardi=1,mercredi=2,jeudi=3,vendredi=4,samedi=5,dimanche=6
- preferred_hours : matin=[8,12], après-midi=[13,18], soir=[17,20], après le travail=[18,20], null si non précisé
- message : résume en 1 phrase ce que tu as compris"""

    # ── Gemini ────────────────────────────────────────────────
    google_key = os.environ.get("GOOGLE_API_KEY","")
    if google_key.startswith("AIza"):
        import requests as _req
        prompt = SYSTEM + f"\n\nMessage : {req.message}"
        for model in ["gemini-2.5-flash","gemini-2.5-pro","gemini-2.0-flash-lite","gemini-2.0-flash"]:
            try:
                r = _req.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={google_key}",
                    json={"contents":[{"parts":[{"text":prompt}]}]}, timeout=15)
                if r.status_code == 404: continue
                if r.status_code == 403:
                    print(f"[Gemini] 403 — active 'Generative Language API' dans Google Cloud Console")
                    break
                if r.status_code in (503, 429):
                    print(f"[Gemini/{model}] {r.status_code} — essai suivant...")
                    continue
                r.raise_for_status()
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                text = text.strip().replace("```json","").replace("```","").strip()
                parsed = json.loads(text)
                print(f"[Gemini/{model}] date={parsed.get('preferred_date')} day={parsed.get('preferred_day_num')} spec={parsed.get('specialty')}")
                return parsed
            except json.JSONDecodeError as e:
                raise HTTPException(status_code=502, detail=f"JSON invalide Gemini : {e}")
            except Exception as e:
                print(f"[Gemini/{model}] {e}"); continue
        raise HTTPException(status_code=502,
            detail="Gemini inaccessible — vérifie GOOGLE_API_KEY et active 'Generative Language API'")

    # ── Claude fallback ───────────────────────────────────────
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY","")
    if anthropic_key.startswith("sk-ant"):
        try:
            from anthropic import Anthropic
            msgs = req.history + [{"role":"user","content":req.message}]
            resp = Anthropic(api_key=anthropic_key).messages.create(
                model="claude-sonnet-4-20250514", max_tokens=500, system=SYSTEM, messages=msgs)
            text = resp.content[0].text.strip().replace("```json","").replace("```","").strip()
            parsed = json.loads(text)
            print(f"[Claude] date={parsed.get('preferred_date')} day={parsed.get('preferred_day_num')}")
            return parsed
        except Exception as e:
            print(f"[Claude] erreur : {e}")

    raise HTTPException(status_code=503,
        detail="Aucune IA disponible. Configure GOOGLE_API_KEY dans .env")

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
            with open("slots.json") as f: data = json.load(f)
            save_crawl_to_db(db,
                practitioners_data=data.get("practitioners",[]),
                slots_data=data.get("slots",[]),
                specialty=req.specialty, city=req.location)
            return {"status":"success","specialty":req.specialty,"location":req.location,
                    "slots_count":len(data.get("slots",[])),
                    "practitioners":len(data.get("practitioners",[])),
                    "message":f"✅ {len(data.get('slots',[]))} créneaux sauvegardés"}
        except Exception as e:
            print(f"[Crawl] Erreur DB : {e}")
    return {"status":"error","message":"slots.json introuvable"}

# ── Planity ───────────────────────────────────────────────────

class PlanityRequest(BaseModel):
    category: str = "coiffeur"
    location: str = "Paris"

@app.post("/crawl/planity")
def crawl_planity(req: PlanityRequest):
    """Lance le crawler Planity en arrière-plan."""
    def run():
        subprocess.run([sys.executable, "planity_crawler.py", req.category, req.location],
                       capture_output=False)
    threading.Thread(target=run, daemon=True).start()
    return {"status": "started", "category": req.category, "location": req.location}

@app.post("/crawl/planity/auto")
def crawl_planity_auto(req: PlanityRequest):
    """Lance le crawler Planity et retourne les résultats."""
    subprocess.run([sys.executable, "planity_crawler.py", req.category, req.location],
                   capture_output=False)
    if os.path.exists("planity_slots.json"):
        try:
            with open("planity_slots.json", encoding="utf-8") as f:
                data = json.load(f)
            return {
                "status":    "success",
                "category":  req.category,
                "location":  req.location,
                "slots_count":    len(data.get("slots", [])),
                "practitioners":  len(data.get("practitioners", [])),
            }
        except Exception as e:
            print(f"[Planity] Erreur : {e}")
    return {"status": "error", "message": "planity_slots.json introuvable"}

@app.post("/recommend/planity")
def recommend_planity(req: RecommendRequest):
    """Recommande les meilleurs créneaux Planity — scoring Calendar+Maps si dispo."""
    weights   = build_weights(req.preferences)
    engine    = ScoringEngine(weights)
    optimizer = Optimizer(engine)

    try:
        from planity_client import load_planity_data, PlanityAdapter, PlanityUserPreferences
        pros, raw_slots = load_planity_data("planity_slots.json")
        if not raw_slots:
            return {"no_slots": True, "message": "Aucun créneau Planity disponible.",
                    "best": None, "ranked": [], "total_slots_analyzed": 0,
                    "practitioners_found": 0, "weights_used": weights.as_dict(),
                    "data_source": "planity", "practitioners_map": {}, "warning": None}

        prac_map = {p.name: p.profile_url for p in pros}

        # ── Scoring Calendar + Maps (identique à /recommend Doctolib) ──
        scoring_source = "mock"
        scoring_slots  = []

        try:
            scheduler = _scheduler
            if not scheduler:
                raise Exception("CalendarScheduler non disponible")

            scheduler.user_location = req.user_origin if req.user_origin else req.location

            # Convertit les slots Planity en format enrichi pour CalendarScheduler
            # On réutilise PlanityAdapter._parse_date pour normaliser les dates
            adapter_parser = PlanityAdapter(PlanityUserPreferences())
            pro_map_by_id = {p.id: p for p in pros}

            raw_slots_enriched = []
            for rs in raw_slots:
                if not rs.start_date:
                    continue
                try:
                    dt = adapter_parser._parse_date(rs.start_date)
                    iso_date = dt.strftime("%Y-%m-%dT%H:%M:%S")
                except Exception:
                    continue
                pro = rs.pro
                raw_slots_enriched.append({
                    "start_date": iso_date,
                    "address":    pro.address or req.location,
                    "name":       pro.name,
                })

            scoring_slots = scheduler.score_slots(
                raw_slots_enriched,
                preferred_hours = [req.preferences.preferred_hours_start, req.preferences.preferred_hours_end],
                preferred_day   = req.preferences.preferred_day,
                preferred_date  = req.preferences.preferred_date,
                location        = req.location,
            )
            scoring_source = "calendar+maps"
            print(f"[Planity] Scoring Calendar+Maps : {len(scoring_slots)} slots")

        except Exception as e:
            print(f"[Planity] Fallback scoring mock : {e}")
            # Fallback PlanityAdapter (scoring estimé sans Calendar/Maps)
            up = PlanityUserPreferences(
                preferred_hours=(req.preferences.preferred_hours_start, req.preferences.preferred_hours_end),
                preferred_day=req.preferences.preferred_day,
                preferred_date=req.preferences.preferred_date,
            )
            scoring_slots  = PlanityAdapter(up).convert(raw_slots)
            scoring_source = "mock"

        # ── Vérification finale ──────────────────────────────────
        if not scoring_slots:
            day_names = {0:"lundi",1:"mardi",2:"mercredi",3:"jeudi",4:"vendredi",5:"samedi",6:"dimanche"}
            if req.preferences.preferred_date:
                warning = f"Aucun créneau disponible le {req.preferences.preferred_date}."
            elif req.preferences.preferred_day is not None:
                warning = f"Aucun créneau disponible le {day_names.get(req.preferences.preferred_day, 'ce jour')}."
            else:
                warning = "Aucun créneau disponible."
            return {
                "no_slots": True, "message": warning,
                "best": None, "ranked": [], "total_slots_analyzed": 0,
                "practitioners_found": len(pros), "weights_used": weights.as_dict(),
                "data_source": "planity", "scoring_source": scoring_source,
                "practitioners_map": prac_map, "warning": warning,
            }

        ranked = optimizer.rank(scoring_slots)
        top    = ranked[:req.top_n]
        print(f"[Planity] Ranked {len(ranked)} slots — best: {ranked[0].slot.time} (score={ranked[0].total:.4f})")

        # Récupère les conflits Calendar pour les créneaux classés
        conflicts_map = {}
        if scoring_source == "calendar+maps" and hasattr(scheduler, 'slot_conflicts'):
            for d in top:
                label = d.slot.time
                if label in scheduler.slot_conflicts:
                    conflicts_map[label] = scheduler.slot_conflicts[label]

        return {
            "best":                 to_slot_result(0, ranked[0]).dict(),
            "ranked":               [to_slot_result(i, d).dict() for i, d in enumerate(top)],
            "total_slots_analyzed": len(scoring_slots),
            "practitioners_found":  len(pros),
            "weights_used":         weights.as_dict(),
            "data_source":          "planity",
            "scoring_source":       scoring_source,
            "practitioners_map":    prac_map,
            "conflicts":            conflicts_map,
            "warning":              None,
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ── Book ──────────────────────────────────────────────────────

_book_state: dict = {"status": "idle", "result": None}

@app.post("/book")
def book_appointment(req: BookRequest):
    global _book_state
    if _book_state["status"] == "running":
        return {"status":"running","message":"Booking déjà en cours..."}
    _book_state = {"status":"running","result":None}
    profile_url=req.profile_url; slot_datetime=req.slot_datetime
    is_new_patient=req.is_new_patient; motive_keyword=req.motive_keyword
    is_teleconsult=req.is_teleconsult; cwd=os.getcwd()

    def run_booking():
        global _book_state
        try:
            script = f"""import sys; sys.path.insert(0, r'{cwd}')
from doctolib_booking import DoctolibBooker
result = DoctolibBooker(headless=False).book(
    profile_url={repr(profile_url)}, slot_datetime={repr(slot_datetime)},
    is_new_patient={repr(is_new_patient)}, motive_keyword={repr(motive_keyword)},
    is_teleconsult={repr(is_teleconsult)})
print("RESULT:" + str(result))
"""
            import tempfile
            tmp = tempfile.NamedTemporaryFile(mode='w',suffix='.py',delete=False,encoding='utf-8')
            tmp.write(script); tmp.close()
            r = subprocess.run([sys.executable,tmp.name],capture_output=True,text=True,timeout=180)
            os.unlink(tmp.name)
            output = r.stdout + r.stderr
            if "RESULT:" in output:
                import ast
                _book_state = {"status":"done","result":ast.literal_eval(output.split("RESULT:")[-1].strip().split("\n")[0])}
            else:
                _book_state = {"status":"error","result":{"status":"error","message":output[-300:]}}
        except Exception as e:
            _book_state = {"status":"error","result":{"status":"error","message":str(e)}}

    threading.Thread(target=run_booking, daemon=True).start()
    return {"status":"running","message":"Navigateur ouvert — réservation en cours..."}

@app.get("/book/status")
def book_status():
    return {"booking_state":_book_state["status"],"result":_book_state.get("result")}

# ── Auth ──────────────────────────────────────────────────────

_login_state: dict = {"status":"idle","error":""}

@app.post("/auth/login")
def auth_login(req: LoginRequest):
    global _login_state
    if _login_state["status"] == "running":
        return {"status":"running","message":"Navigateur déjà ouvert"}
    _login_state = {"status":"running","error":""}
    email=req.email; password=req.password; cwd=os.getcwd()

    def run_login():
        global _login_state
        try:
            script = f"""import sys; sys.path.insert(0, r'{cwd}')
from doctolib_auth import login
try:
    cookies = login(email={repr(email)}, password={repr(password)}, headless=False)
    print("SUCCESS:" + str(len(cookies)))
except Exception as e:
    print("ERROR:" + str(e))
"""
            import tempfile
            tmp = tempfile.NamedTemporaryFile(mode='w',suffix='.py',delete=False,encoding='utf-8')
            tmp.write(script); tmp.close()
            r = subprocess.run([sys.executable,tmp.name],capture_output=True,text=True,timeout=120)
            os.unlink(tmp.name)
            output = r.stdout + r.stderr
            if "SUCCESS:" in output:
                _login_state = {"status":"done","error":""}
            else:
                _login_state = {"status":"error","error":output.split("ERROR:")[-1].strip() if "ERROR:" in output else output[-200:]}
        except Exception as e:
            _login_state = {"status":"error","error":str(e)}

    threading.Thread(target=run_login, daemon=True).start()
    return {"status":"running","message":"Navigateur ouvert — connecte-toi"}

@app.get("/auth/status")
def auth_status():
    from doctolib_auth import session_info
    info = session_info()
    info["login_state"] = _login_state["status"]
    info["login_error"] = _login_state.get("error","")
    return info

@app.post("/auth/logout")
def auth_logout():
    global _login_state
    _login_state = {"status":"idle","error":""}
    from doctolib_auth import logout
    logout()
    return {"status":"success","message":"Session supprimée"}

# ── Historique & Stats ────────────────────────────────────────

@app.get("/history")
def booking_history(email: str = None, db: Session = Depends(get_db)):
    bookings = get_booking_history(db, user_email=email)
    return [{"id":b.id,"profile_url":b.profile_url,"slot_datetime":b.slot_datetime,
             "user_email":b.user_email,"status":b.status,"message":b.message,
             "created_at":b.created_at.isoformat() if b.created_at else ""} for b in bookings]

@app.get("/db/stats")
def db_stats(db: Session = Depends(get_db)):
    from database import Practitioner as P, Slot as S, Booking as B
    return {"practitioners":db.query(P).count(),"slots":db.query(S).count(),
            "bookings":db.query(B).count(),"db_file":"smartrdv.db"}