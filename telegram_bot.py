"""
SmartRDV — Bot Telegram
========================
Lance : python telegram_bot.py
Nécessite : pip install python-telegram-bot requests
"""
import logging
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ── Config ────────────────────────────────────────────────────
TELEGRAM_TOKEN = "METS_TON_TOKEN_ICI"
API_BASE       = "http://localhost:8000"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── Helpers API ───────────────────────────────────────────────

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

def call_recommend(specialty: str, location: str, preferred_day: int = None,
                   preferred_hours: list = None) -> dict:
    prefs = {
        "preferred_hours_start": preferred_hours[0] if preferred_hours else 9,
        "preferred_hours_end":   preferred_hours[1] if preferred_hours else 18,
        "preferred_day":         preferred_day,
    }
    r = requests.post(f"{API_BASE}/recommend",
        json={
            "specialty":  specialty or "medecin-generaliste",
            "location":   location or "Paris",
            "top_n":      5,
            "preferences": prefs
        },
        timeout=20)
    r.raise_for_status()
    return r.json()

def call_book(profile_url: str, slot_time: str) -> dict:
    r = requests.post(f"{API_BASE}/book", json={
        "profile_url":    profile_url,
        "slot_datetime":  slot_time,
        "is_new_patient": True,
    }, timeout=10)
    r.raise_for_status()
    return r.json()

def call_book_status() -> dict:
    r = requests.get(f"{API_BASE}/book/status", timeout=5)
    r.raise_for_status()
    return r.json()

# ── État des conversations ────────────────────────────────────
user_sessions = {}

def get_session(user_id: int) -> dict:
    if user_id not in user_sessions:
        user_sessions[user_id] = {"history": [], "nlp": {}, "data": {}}
    return user_sessions[user_id]

# ── Commandes ─────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_sessions.pop(update.effective_user.id, None)
    await update.message.reply_text(
        f"👋 Bonjour {user.first_name} ! Je suis *SmartRDV*, ton assistant médical IA.\n\n"
        "Dis-moi simplement ce dont tu as besoin :\n"
        "• _Je veux un gynécologue vendredi soir_\n"
        "• _Dermatologue demain matin à Paris_\n"
        "• _Cardiologue après le travail_\n\n"
        "Commandes disponibles :\n"
        "/start — Recommencer\n"
        "/status — État du backend\n"
        "/annuler — Annuler la recherche en cours",
        parse_mode="Markdown"
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        r = requests.get(f"{API_BASE}/status", timeout=5)
        d = r.json()
        if d.get("has_data"):
            msg = (f"✅ Backend actif\n"
                   f"📋 {d['slots']} créneaux ({d['specialty']} — {d['location']})\n"
                   f"👨‍⚕️ {d['practitioners']} praticiens")
        else:
            msg = "✅ Backend actif\n⚠️ Pas de données crawlées"
        await update.message.reply_text(msg)
    except:
        await update.message.reply_text("❌ Backend inaccessible — lance uvicorn !")

async def cmd_annuler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_sessions.pop(update.effective_user.id, None)
    await update.message.reply_text("🔄 Recherche annulée. Dis-moi ce que tu cherches !")

# ── Message principal ─────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    text    = update.message.text.strip()

    await update.message.chat.send_action("typing")

    # ── Appel NLP ─────────────────────────────────────────────
    try:
        nlp = call_chat(text, session["history"])
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur NLP : {e}\nVérifie que uvicorn tourne.")
        return

    session["history"].append({"role": "user", "content": text})
    session["history"].append({"role": "assistant", "content": str(nlp)})
    session["nlp"] = nlp

    # ── Pas un booking ────────────────────────────────────────
    if nlp.get("intent") != "book":
        await update.message.reply_text(nlp.get("message", "Comment puis-je t'aider ?"))
        return

    # ── Booking intent — valeurs avec fallback ────────────────
    specialty = nlp.get("specialty") or "medecin-generaliste"
    location  = nlp.get("location") or "Paris"

    await update.message.reply_text(
        f"🔍 {nlp.get('message', '')}\n\n⏳ Je crawle Doctolib pour *{specialty}* à *{location}*...",
        parse_mode="Markdown"
    )

    # ── Crawl ─────────────────────────────────────────────────
    try:
        crawl = call_crawl_auto(specialty, location)
        slots_count = crawl.get("slots_count", 0)
        await update.message.reply_text(f"✅ {slots_count} créneaux trouvés — analyse en cours...")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Crawl échoué : {e}\nJ'utilise les données existantes.")

    # ── Recommend ─────────────────────────────────────────────
    try:
        data = call_recommend(
            specialty       = specialty,
            location        = location,
            preferred_day   = nlp.get("preferred_day_num"),
            preferred_hours = nlp.get("preferred_hours"),
        )
        session["data"] = data
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur recommandation : {e}")
        return

    # ── Affiche les résultats ──────────────────────────────────
    best   = data["best"]
    ranked = data.get("ranked", [])[:5]

    keyboard = []
    for i, slot in enumerate(ranked):
        label = slot["label"]
        score = int(slot["total_score"] * 100)
        emoji = "⭐" if i == 0 else f"#{i+1}"
        keyboard.append([InlineKeyboardButton(
            f"{emoji} {label} — score {score}",
            callback_data=f"book:{i}"
        )])
    keyboard.append([InlineKeyboardButton("❌ Annuler", callback_data="cancel")])

    warning = data.get("warning") or ""
    msg  = f"🏥 *Meilleur créneau trouvé :*\n\n"
    msg += f"👨‍⚕️ *{best['label']}*\n"
    msg += f"📊 Score : {int(best['total_score']*100)}/100 _(plus c'est bas, mieux c'est)_\n"
    if warning:
        msg += f"\n⚠️ {warning}\n"
    msg += f"\n_{data['total_slots_analyzed']} créneaux analysés — source : {data['data_source']}_\n\n"
    msg += "Clique sur un créneau pour le réserver :"

    await update.message.reply_text(
        msg,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ── Callback boutons ──────────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id
    session = get_session(user_id)
    data    = query.data

    await query.answer()

    if data == "cancel":
        user_sessions.pop(user_id, None)
        await query.edit_message_text("❌ Réservation annulée.")
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

        best_name   = label.split(" — ")[0].strip()
        profile_url = ""
        prac_map    = session.get("data", {}).get("practitioners_map", {})
        if best_name in prac_map:
            profile_url = prac_map[best_name]

        if not profile_url:
            spec = nlp.get("specialty") or "medecin-generaliste"
            loc  = (nlp.get("location") or "Paris").lower()
            slug = best_name.lower().replace("dr. ","").replace("dr ","").replace(" ","-")
            profile_url = f"https://www.doctolib.fr/{spec}/{loc}/{slug}"

        await query.edit_message_text(
            f"🚀 Réservation lancée pour *{label}*...\n\n"
            f"Un navigateur Doctolib va s'ouvrir sur la machine qui fait tourner le backend.\n"
            f"⏳ J'attends la confirmation...",
            parse_mode="Markdown"
        )

        try:
            call_book(profile_url, slot_time)
        except Exception as e:
            await ctx.bot.send_message(user_id, f"❌ Erreur booking : {e}")
            return

        import asyncio
        for _ in range(36):
            await asyncio.sleep(5)
            try:
                status = call_book_status()
                state  = status.get("booking_state")
                result = status.get("result", {})
                if state == "done":
                    if result.get("status") == "success":
                        await ctx.bot.send_message(user_id,
                            f"✅ *{result.get('message', 'Rendez-vous confirmé !')}*",
                            parse_mode="Markdown")
                    else:
                        await ctx.bot.send_message(user_id,
                            f"⚠️ {result.get('message', 'Erreur lors de la réservation')}")
                    return
                elif state == "error":
                    await ctx.bot.send_message(user_id,
                        f"❌ {result.get('message', 'Erreur inconnue')}")
                    return
            except:
                pass

        await ctx.bot.send_message(user_id, "⏰ Timeout — vérifie le navigateur manuellement.")

# ── Main ──────────────────────────────────────────────────────

def main():
    print("🤖 SmartRDV Bot Telegram démarré...")
    print(f"📡 Backend : {API_BASE}")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("annuler", cmd_annuler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    print("✅ Bot en écoute — appuie sur Ctrl+C pour arrêter")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
