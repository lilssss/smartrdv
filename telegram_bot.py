"""
SmartRDV — Bot Telegram
========================
Flux 1 (passif)  : scan Gmail toutes les 30s → notifications automatiques
Flux 2 (actif)   : messages naturels → réservation médicale Doctolib

Lancement :
    Terminal 1 : python -m uvicorn main:app --reload --port 8000
    Terminal 2 : python telegram_bot.py

Test :
    1. /start dans Telegram → le bot répond et démarre le scan Gmail
    2. Envoie-toi un mail de cinéma/restaurant → notif Telegram dans les 30s
    3. Écris "gynécologue demain matin" → recherche Doctolib
"""
import asyncio
import logging
import os
import json
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ── Config ────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "") #— à créer dans BotFather et mettre dans .env
API_BASE       = "http://localhost:8000"
SCAN_INTERVAL  = 30  # secondes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── État global ───────────────────────────────────────────────
subscribed_users   = set()   # user_ids abonnés aux notifs Gmail
user_sessions      = {}      # sessions conversations RDV
pending_cal_events = {}      # id → event, pour les boutons calendrier
pending_searches   = {}      # user_id → nlp dict, recherche en attente de position
user_locations     = {}      # user_id → "adresse" (position partagée)

def get_session(user_id: int) -> dict:
    if user_id not in user_sessions:
        user_sessions[user_id] = {"history": [], "nlp": {}, "data": {}}
    return user_sessions[user_id]


# ── Helpers API FastAPI ───────────────────────────────────────

def call_chat(message: str, history: list = []) -> dict:
    r = requests.post(f"{API_BASE}/chat", json={"message": message, "history": history}, timeout=20)
    r.raise_for_status()
    return r.json()

def call_crawl_auto(specialty: str, location: str = "Paris") -> dict:
    r = requests.post(f"{API_BASE}/crawl/auto",
        json={"specialty": specialty or "medecin-generaliste", "location": location or "Paris"},
        timeout=300)
    r.raise_for_status()
    return r.json()

def call_recommend(specialty: str, location: str, preferred_day=None,
                   preferred_hours=None, preferred_date=None, user_origin: str = "") -> dict:
    prefs = {
        "preferred_hours_start": preferred_hours[0] if preferred_hours else 9,
        "preferred_hours_end":   preferred_hours[1] if preferred_hours else 18,
        "preferred_day":         preferred_day,
        "preferred_date":        preferred_date,
    }
    r = requests.post(f"{API_BASE}/recommend",
        json={"specialty":   specialty or "medecin-generaliste",
              "location":    location or "Paris",
              "user_origin": user_origin,
              "top_n": 5, "preferences": prefs},
        timeout=60)
    r.raise_for_status()
    return r.json()

def call_crawl_planity(category: str, location: str = "Paris") -> dict:
    r = requests.post(f"{API_BASE}/crawl/planity/auto",
        json={"category": category or "coiffeur", "location": location or "Paris"},
        timeout=300)
    r.raise_for_status()
    return r.json()

def call_recommend_planity(specialty: str, location: str, preferred_day=None,
                            preferred_hours=None, preferred_date=None, user_origin: str = "") -> dict:
    prefs = {
        "preferred_hours_start": preferred_hours[0] if preferred_hours else 9,
        "preferred_hours_end":   preferred_hours[1] if preferred_hours else 18,
        "preferred_day":         preferred_day,
        "preferred_date":        preferred_date,
    }
    r = requests.post(f"{API_BASE}/recommend/planity",
        json={"specialty": specialty or "coiffeur",
              "location":  location or "Paris",
              "user_origin": user_origin,
              "top_n": 5, "preferences": prefs},
        timeout=60)
    r.raise_for_status()
    return r.json()

def call_book(profile_url: str, slot_time: str) -> dict:
    r = requests.post(f"{API_BASE}/book",
        json={"profile_url": profile_url, "slot_datetime": slot_time, "is_new_patient": True},
        timeout=10)
    r.raise_for_status()
    return r.json()

def call_book_status() -> dict:
    r = requests.get(f"{API_BASE}/book/status", timeout=5)
    r.raise_for_status()
    return r.json()


# ── Scanner Gmail ─────────────────────────────────────────────

def scan_new_emails() -> list:
    """Retourne les nouveaux événements Gmail détectés depuis le dernier scan."""
    try:
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        import sys
        sys.path.insert(0, os.getcwd())
        from gmail_parser import get_gmail_service, fetch_recent_emails, parse_email_with_gemini

        service = get_gmail_service()
        emails  = fetch_recent_emails(service, max_results=10)

        events = []
        for email in emails:
            result = parse_email_with_gemini(email)
            if result and result.get("type") not in ("rien", None):
                events.append(result)
                print(f"[Gmail] Nouveau : [{result['type']}] {result['titre']}")

        return events
    except Exception as e:
        log.error(f"[Gmail scan] Erreur : {e}")
        return []


def format_event_message(event: dict) -> str:
    """Formate un événement pour Telegram."""
    emoji = {
        "cinema": "🎬", "meeting": "📅", "transport": "🚂",
        "restaurant": "🍽️", "medical": "🏥", "livraison": "📦",
        "concert_evenement": "🎵", "autre_reservation": "📋",
    }.get(event.get("type", ""), "📩")

    msg  = f"{emoji} *Nouvel événement détecté dans tes mails*\n\n"
    msg += f"📌 *{event.get('titre', 'Événement')}*\n"
    if event.get("date"):  msg += f"📅 {event['date']}"
    if event.get("heure"): msg += f" à {event['heure']}"
    if event.get("date") or event.get("heure"): msg += "\n"
    if event.get("lieu"):  msg += f"📍 {event['lieu']}\n"
    if event.get("lien"):  msg += f"🔗 {event['lien']}\n"
    if event.get("details"): msg += f"\n_{event['details']}_\n"
    return msg


# ── Scan en arrière-plan ──────────────────────────────────────

async def gmail_scan_loop(app):
    """Tourne en arrière-plan, scanne Gmail toutes les 30s."""
    await asyncio.sleep(3)
    print(f"[Gmail] Scan automatique démarré — toutes les {SCAN_INTERVAL}s")

    while True:
        await asyncio.sleep(SCAN_INTERVAL)

        if not subscribed_users:
            continue

        new_events = scan_new_emails()
        if not new_events:
            continue

        for user_id in list(subscribed_users):
            for i, event in enumerate(new_events):
                try:
                    msg = format_event_message(event)
                    action = event.get("action_suggérée", "rien")
                    # Stocke l'événement en mémoire avec un ID unique
                    import uuid
                    ev_id = str(uuid.uuid4())[:8]
                    pending_cal_events[ev_id] = event
                    buttons = []
                    if action == "ajouter_calendrier":
                        buttons.append([InlineKeyboardButton("📅 Ajouter au calendrier", callback_data=f"cal:{ev_id}")])
                    elif action == "reserver":
                        buttons.append([InlineKeyboardButton("🎟️ Réserver", callback_data=f"reserve:{ev_id}")])
                    buttons.append([InlineKeyboardButton("❌ Ignorer", callback_data=f"ignore:{ev_id}")])

                    await app.bot.send_message(
                        user_id, msg,
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(buttons)
                    )
                except Exception as e:
                    log.error(f"[Notif] Erreur user {user_id} : {e}")


# ── Commandes ─────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_sessions.pop(user.id, None)
    subscribed_users.add(user.id)
    await update.message.reply_text(
        f"👋 Bonjour *{user.first_name}* ! Je suis *kAIros*, ton assistant IA de planning.\n\n"
        "Je gère tout ton agenda en parallèle :\n\n"
        "📧 *Notifications automatiques* — Je surveille tes mails toutes les 30s et te notifie pour chaque confirmation "
        "(cinéma, restaurant, concert, meeting, livraison...)\n\n"
        "🏥 *RDV médical* — Sur Doctolib, avec scoring intelligent basé sur ton calendrier et tes trajets :\n"
        "• _Je veux un gynécologue vendredi soir_\n"
        "• _Cardiologue le 25 avril après le travail_\n\n"
        "💇 *Bien-être & beauté* — Sur Planity, même logique :\n"
        "• _Je veux un coiffeur samedi matin_\n"
        "• _Massage dimanche après-midi_\n\n"
        "📅 *Google Calendar* — J'ajoute automatiquement tes événements et détecte les conflits\n\n"
        "Commandes :\n"
        "/status — État du système\n"
        "/scan — Scanner les mails maintenant\n"
        "/annuler — Annuler une recherche en cours",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("📍 Partager ma position", request_location=True)]],
            resize_keyboard=True, one_time_keyboard=True
        )
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subscribed_users.add(update.effective_user.id)
    try:
        r = requests.get(f"{API_BASE}/status", timeout=5)
        d = r.json()
        backend = f"✅ Backend actif — {d.get('slots',0)} créneaux" if d.get("has_data") else "✅ Backend actif"
    except:
        backend = "❌ Backend inaccessible"
    await update.message.reply_text(
        f"{backend}\n📧 Scan Gmail actif toutes les {SCAN_INTERVAL}s\n"
        f"👥 {len(subscribed_users)} utilisateur(s) abonné(s)"
    )

async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Force un scan Gmail immédiat."""
    subscribed_users.add(update.effective_user.id)
    await update.message.reply_text("🔍 Scan Gmail en cours...")
    events = scan_new_emails()
    if not events:
        await update.message.reply_text("📭 Aucun nouvel événement détecté.")
        return
    for i, event in enumerate(events):
        msg = format_event_message(event)
        action = event.get("action_suggérée", "rien")
        buttons = []
        if action == "ajouter_calendrier":
            buttons.append([InlineKeyboardButton("📅 Ajouter au calendrier", callback_data=f"cal:{i}")])
        buttons.append([InlineKeyboardButton("❌ Ignorer", callback_data=f"ignore:{i}")])
        await update.message.reply_text(msg, parse_mode="Markdown",
                                        reply_markup=InlineKeyboardMarkup(buttons))

async def cmd_annuler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_sessions.pop(update.effective_user.id, None)
    await update.message.reply_text("🔄 Recherche annulée.")


# ── Localisation GPS ─────────────────────────────────────────

async def handle_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Reçoit la position GPS partagée depuis Telegram."""
    user_id  = update.effective_user.id
    location = update.message.location
    subscribed_users.add(user_id)

    # Convertit coordonnées → adresse via Maps API
    coords = f"{location.latitude},{location.longitude}"
    address = None

    maps_key = os.environ.get("MAPS_API_KEY", "")
    if maps_key:
        try:
            import requests as _r
            r = _r.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"latlng": coords, "key": maps_key, "language": "fr"},
                timeout=10
            )
            data = r.json()
            results = data.get("results", [])
            print(f"[Geocode] Status: {data.get('status')} — {len(results)} résultats")
            if results:
                address = results[0].get("formatted_address")
                print(f"[Geocode] ✅ {coords} → {address}")
            else:
                print(f"[Geocode] ⚠️ Aucun résultat — réponse : {data}")
        except Exception as e:
            print(f"[Geocode] Erreur : {e}")

    # Les coordonnées GPS fonctionnent directement avec Distance Matrix API
    # Utilise l'adresse geocodée si disponible, sinon les coordonnées GPS brutes
    user_locations[user_id] = (address if address and address not in ("None", None) else coords) or ""
    user_locations[f"{user_id}_doctolib"] = "Paris"
    print(f"[Geocode] Origin Maps = {user_locations[user_id]}")
    display = address if address and address != "None" else f"📌 {coords}"
    await update.message.reply_text(
        f"📍 Position enregistrée : *{display}*\nJe l'utiliserai pour calculer les trajets.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )

    # Si une recherche était en attente → la lancer maintenant
    if user_id in pending_searches:
        nlp = pending_searches.pop(user_id)
        await _run_search(update, ctx, nlp, user_id, address)


# ── Messages — RDV médical ────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    subscribed_users.add(user_id)
    session = get_session(user_id)
    text    = update.message.text.strip()

    # Traite le partage de localisation texte (ex: "je suis à Montmartre")
    if any(w in text.lower() for w in ["je suis à", "je suis a", "ma position", "je me trouve"]):
        user_locations[user_id] = text
        await update.message.reply_text(f"📍 Position enregistrée : *{text}*\nJe l'utiliserai pour calculer les trajets.", parse_mode="Markdown")
        return

    await update.message.chat.send_action("typing")

    try:
        nlp = call_chat(text, session["history"])
        nlp["_raw_message"] = text  # Stocker pour fallback date
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur NLP : {e}\nVérifie que uvicorn tourne.")
        return

    session["history"].append({"role": "user", "content": text})
    session["history"].append({"role": "assistant", "content": str(nlp)})
    session["nlp"] = nlp

    if nlp.get("intent") != "book":
        await update.message.reply_text(nlp.get("message", "Comment puis-je t'aider ?"))
        return

    specialty = nlp.get("specialty") or "medecin-generaliste"
    location  = nlp.get("location") or user_locations.get(user_id, "Paris")

    # Si pas de position → stocker la recherche et attendre
    if user_id not in user_locations:
        pending_searches[user_id] = nlp
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("📍 Partager ma position", request_location=True)]],
            resize_keyboard=True, one_time_keyboard=True
        )
        platform = nlp.get("platform", "doctolib")
        dest_label = "professionnels" if platform == "planity" else "médecins"
        await update.message.reply_text(
            f"📍 Partage ta position pour que je calcule les trajets vers les {dest_label}.\n"
            f"_(La recherche démarrera dès que tu auras partagé ta position)_",
            parse_mode="Markdown", reply_markup=kb
        )
        return

    await _run_search(update, ctx, nlp, user_id, location)


async def _run_search(update, ctx, nlp, user_id, location=None):
    """Lance le crawl + recommend après avoir reçu la position."""
    from datetime import date as _date
    session   = get_session(user_id)
    specialty = nlp.get("specialty") or "medecin-generaliste"
    # Pour Doctolib on cherche à Paris, pour Maps on utilise la vraie position
    doctolib_location = user_locations.get(f"{user_id}_doctolib") or nlp.get("location") or "Paris"
    maps_origin       = location or user_locations.get(user_id, "Paris")
    location          = doctolib_location

    # ── Fallback date : si Gemini n'a pas extrait preferred_date mais le message
    # contient des mots signifiant "aujourd'hui", on force la date du jour
    if not nlp.get("preferred_date"):
        msg_lower = (nlp.get("_raw_message") or "").lower()
        TODAY_KEYWORDS = [
            "aujourd'hui", "ajourd'hui", "ce matin", "cet après-midi",
            "cet apres-midi", "ce soir", "tout de suite", "maintenant",
            "dans la journée", "dans la soirée",
        ]
        if any(kw in msg_lower for kw in TODAY_KEYWORDS):
            nlp["preferred_date"] = _date.today().isoformat()

    platform = nlp.get("platform", "doctolib")
    platform_name = "Planity" if platform == "planity" else "Doctolib"
    await update.message.reply_text(
        f"🔍 {nlp.get('message', '')}\n\n⏳ Je crawle *{platform_name}* pour *{specialty}* à *{location}*...",
        parse_mode="Markdown"
    )

    try:
        if platform == "planity":
            crawl = call_crawl_planity(specialty, location)
        else:
            crawl = call_crawl_auto(specialty, location)
        await update.message.reply_text(f"✅ {crawl.get('slots_count', 0)} créneaux trouvés — analyse en cours...")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Crawl échoué : {e}\nJ'utilise les données existantes.")

    try:
        if platform == "planity":
            data = call_recommend_planity(
                specialty       = specialty,
                location        = location,
                preferred_day   = nlp.get("preferred_day_num"),
                preferred_hours = nlp.get("preferred_hours"),
                preferred_date  = nlp.get("preferred_date"),
                user_origin     = user_locations.get(user_id) or "",
            )
        else:
            data = call_recommend(
                specialty       = specialty,
                location        = location,
                preferred_day   = nlp.get("preferred_day_num"),
                preferred_hours = nlp.get("preferred_hours"),
                preferred_date  = nlp.get("preferred_date"),
                user_origin     = user_locations.get(user_id) or "",
            )
        session["data"] = data
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur recommandation : {e}")
        return

    # ── Pas de créneaux disponibles ───────────────────────────
    if data.get("no_slots"):
        msg = data.get("message", "Aucun créneau disponible.")
        session["pending_retry_nlp"] = nlp
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Chercher sans contrainte de date", callback_data=f"retry_search:{specialty}:{location}")],
            [InlineKeyboardButton("❌ Annuler", callback_data="cancel")],
        ])
        await update.message.reply_text(
            f"😕 *{msg}*\n\nVeux-tu que je cherche le prochain créneau disponible ?",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return

    best   = data["best"]
    ranked = data.get("ranked", [])[:5]
    keyboard = []
    for i, slot in enumerate(ranked):
        try:
            label = slot["label"].encode('latin-1').decode('utf-8')
        except:
            label = slot["label"]
        score = int(slot["total_score"] * 100)
        emoji = "⭐" if i == 0 else f"#{i+1}"
        keyboard.append([InlineKeyboardButton(f"{emoji} {label} — score {score}", callback_data=f"book:{i}")])
    keyboard.append([InlineKeyboardButton("❌ Annuler", callback_data="cancel")])

    warning = data.get("warning") or ""
    def fix_encoding(s):
        try:
            return s.encode('latin-1').decode('utf-8')
        except:
            return s

    best_label = fix_encoding(best['label'])
    msg  = f"🏥 *Meilleur créneau trouvé :*\n\n"
    msg += f"👨‍⚕️ *{best_label}*\n"
    msg += f"📊 Score : {int(best['total_score']*100)}/100 _(plus c'est bas, mieux c'est)_\n"
    if warning: msg += f"\n⚠️ {warning}\n"
    msg += f"\n_{data['total_slots_analyzed']} créneaux analysés — source : {data['data_source']}_\n\n"
    msg += "Clique sur un créneau pour le réserver :"

    await update.message.reply_text(msg, parse_mode="Markdown",
                                    reply_markup=InlineKeyboardMarkup(keyboard))


# ── Callbacks ─────────────────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id
    session = get_session(user_id)
    data    = query.data
    await query.answer()

    if data.startswith("ignore:") or data.startswith("reserve:"):
        await query.edit_message_text("✅ Noté !")
        return

    if data.startswith("cal:"):
        await query.edit_message_text("⏳ Vérification du calendrier en cours...")
        try:
            import sys, os
            sys.path.insert(0, os.getcwd())
            from google_calendar import add_event

            ev_id = data.split(":")[1]
            event = pending_cal_events.get(ev_id, {})
            if not event:
                await query.edit_message_text("❌ Événement introuvable — relance un scan.")
                return

            result = add_event(event)

            if result.get("success"):
                await query.edit_message_text(
                    f"✅ *{event.get('titre','Événement')}* ajouté à Google Calendar !\n📅 {event.get('date','')} {event.get('heure','')}",
                    parse_mode="Markdown"
                )
            elif result.get("conflicts"):
                # Conflit détecté — demande confirmation
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Ajouter quand même", callback_data=f"force_cal:{idx}")],
                    [InlineKeyboardButton("❌ Annuler", callback_data="ignore:0")],
                ])
                await query.edit_message_text(
                    result["conflict_message"],
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
            else:
                await query.edit_message_text(f"❌ Erreur : {result.get('error')}")
        except Exception as e:
            await query.edit_message_text(f"❌ Erreur calendrier : {e}")
        return

    if data.startswith("force_cal:"):
        await query.edit_message_text("⏳ Ajout forcé en cours...")
        try:
            import sys, os
            sys.path.insert(0, os.getcwd())
            from google_calendar import _insert_event

            ev_id = data.split(":")[1]
            event = pending_cal_events.get(ev_id, {})
            if not event:
                await query.edit_message_text("❌ Événement introuvable.")
                return

            result = _insert_event(event)
            if result.get("success"):
                await query.edit_message_text(
                    f"✅ *{event.get('titre','Événement')}* ajouté malgré le conflit !\n📅 {event.get('date','')} {event.get('heure','')}",
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text(f"❌ Erreur : {result.get('error')}")
        except Exception as e:
            await query.edit_message_text(f"❌ Erreur : {e}")
        return

    if data == "cancel":
        user_sessions.pop(user_id, None)
        await query.edit_message_text("❌ Réservation annulée.")
        return

    if data.startswith("force_search:"):
        # L'utilisateur veut chercher malgré les conflits Calendar
        nlp_saved = session.get("pending_calendar_nlp", {})
        loc_saved = session.get("pending_calendar_location") or user_locations.get(user_id)
        if not nlp_saved:
            await query.edit_message_text("❌ Session expirée — relance ta recherche.")
            return
        await query.edit_message_text(
            f"⏳ Recherche en cours malgré les conflits...",
        )
        # Injecte un faux update.message pour _run_search
        class _FakeMsg:
            async def reply_text(self, *a, **kw):
                await ctx.bot.send_message(user_id, *a, **kw)
        class _FakeUpdate:
            message = _FakeMsg()
        await _run_search(_FakeUpdate(), ctx, nlp_saved, user_id, loc_saved)
        return

    if data.startswith("retry_search:"):
        parts   = data.split(":")
        spec    = parts[1] if len(parts) > 1 else "medecin-generaliste"
        loc     = parts[2] if len(parts) > 2 else "Paris"
        nlp     = session.get("pending_retry_nlp", {})
        # Retry sans preferred_date ni preferred_day
        nlp_retry = dict(nlp)
        nlp_retry["preferred_date"]    = None
        nlp_retry["preferred_day_num"] = None
        await query.edit_message_text("🔍 Recherche du prochain créneau disponible...")
        await _run_search(query, ctx, nlp_retry, user_id, user_locations.get(user_id))
        return

    if data.startswith("book:"):
        idx    = int(data.split(":")[1])
        ranked = session.get("data", {}).get("ranked", [])
        nlp    = session.get("nlp", {})

        if idx >= len(ranked):
            await query.edit_message_text("❌ Créneau invalide.")
            return

        slot      = ranked[idx]
        label     = slot["label"]
        slot_time = label[-5:] if len(label) >= 5 else "09:00"

        profile_url = ""
        prac_map    = session.get("data", {}).get("practitioners_map", {})
        best_name   = label.split(" — ")[0].strip()
        if best_name in prac_map:
            profile_url = prac_map[best_name]
        if not profile_url:
            for url in prac_map.values():
                if url and url.startswith("http"):
                    profile_url = url
                    break
        if not profile_url:
            spec = nlp.get("specialty") or "medecin-generaliste"
            loc  = (nlp.get("location") or "Paris").lower()
            profile_url = f"https://www.doctolib.fr/{spec}/{loc}"

        await query.edit_message_text(
            f"🚀 Réservation lancée pour *{label}*...\n\n"
            f"Un navigateur Doctolib va s'ouvrir sur le PC.\n⏳ J'attends la confirmation...",
            parse_mode="Markdown"
        )

        try:
            call_book(profile_url, slot_time)
        except Exception as e:
            await ctx.bot.send_message(user_id, f"❌ Erreur booking : {e}")
            return

        for _ in range(36):
            await asyncio.sleep(5)
            try:
                status = call_book_status()
                state  = status.get("booking_state")
                result = status.get("result", {})
                if state == "done":
                    msg = f"✅ *{result.get('message','Confirmé !')}*" if result.get("status") == "success" else f"⚠️ {result.get('message','Erreur')}"
                    await ctx.bot.send_message(user_id, msg, parse_mode="Markdown")
                    return
                elif state == "error":
                    await ctx.bot.send_message(user_id, f"❌ {result.get('message','Erreur inconnue')}")
                    return
            except:
                pass

        await ctx.bot.send_message(user_id, "⏰ Timeout — vérifie le navigateur manuellement.")


# ── Main ──────────────────────────────────────────────────────

def main():
    print("🤖 kAIros Bot Telegram démarré...")
    print(f"📡 Backend : {API_BASE}")
    print(f"📧 Scan Gmail toutes les {SCAN_INTERVAL}s")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("scan",    cmd_scan))
    app.add_handler(CommandHandler("annuler", cmd_annuler))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    async def on_startup(application):
        asyncio.ensure_future(gmail_scan_loop(application))

    app.post_init = on_startup

    print("✅ Bot en écoute — Ctrl+C pour arrêter")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()