"""
Gmail Parser — SmartRDV Assistant
===================================
Lit les mails Gmail et extrait automatiquement :
- Réservations cinéma / spectacles
- Meetings (Zoom, Meet, Teams)
- Billets transport (SNCF, avion)
- Confirmations restaurant
- Confirmations médicales (Doctolib)
- Livraisons
- Concerts / événements

Nécessite :
    pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client requests

Setup OAuth2 :
    1. console.cloud.google.com → Nouveau projet
    2. APIs → Gmail API → Activer
    3. Identifiants → OAuth 2.0 → Desktop App
    4. Télécharger credentials.json dans ce dossier
    5. python gmail_parser.py → autorise dans le navigateur
"""

import os
import json
import base64
import re
from datetime import datetime
from typing import Optional

# Google API
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# NLP via Gemini (même clé que SmartRDV)
import requests as _req

# ── Config ────────────────────────────────────────────────────

SCOPES            = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE  = "gmail_credentials.json"   # téléchargé depuis Google Cloud
TOKEN_FILE        = "gmail_token.json"          # généré automatiquement
MAX_EMAILS        = 50                          # nb de mails à analyser
GOOGLE_API_KEY    = os.environ.get("GOOGLE_API_KEY", "")


# ── Auth Gmail ────────────────────────────────────────────────

def get_gmail_service():
    """Authentifie et retourne le service Gmail."""
    creds = None

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                raise FileNotFoundError(
                    f"Fichier {CREDENTIALS_FILE} introuvable.\n"
                    "Télécharge-le depuis console.cloud.google.com → APIs → Gmail → Identifiants"
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ── Lecture des mails ─────────────────────────────────────────

def get_email_body(msg: dict) -> str:
    """Extrait le corps texte d'un mail Gmail."""
    body = ""
    payload = msg.get("payload", {})

    def extract_parts(parts):
        text = ""
        for part in parts:
            mime = part.get("mimeType", "")
            if mime == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    text += base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
            elif mime.startswith("multipart"):
                text += extract_parts(part.get("parts", []))
        return text

    if payload.get("mimeType", "").startswith("multipart"):
        body = extract_parts(payload.get("parts", []))
    else:
        data = payload.get("body", {}).get("data", "")
        if data:
            body = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")

    return body[:3000]  # Limite pour Gemini


HISTORY_FILE = "gmail_history_id.txt"

def get_last_history_id() -> str:
    if os.path.exists(HISTORY_FILE):
        return open(HISTORY_FILE).read().strip()
    return None

def save_history_id(history_id: str):
    open(HISTORY_FILE, "w").write(str(history_id))

def fetch_email_by_id(service, msg_id: str) -> dict:
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return {
        "id":      msg_id,
        "subject": headers.get("Subject", ""),
        "from":    headers.get("From", ""),
        "date":    headers.get("Date", ""),
        "body":    get_email_body(msg)[:2000],
    }

def fetch_recent_emails(service, max_results: int = MAX_EMAILS) -> list:
    """
    Premier appel : sauvegarde le historyId courant sans analyser les anciens mails.
    Appels suivants : retourne uniquement les NOUVEAUX mails via l'API History.
    """
    emails = []
    last_history_id = get_last_history_id()
    profile = service.users().getProfile(userId="me").execute()
    current_history_id = str(profile.get("historyId", ""))

    if not last_history_id:
        save_history_id(current_history_id)
        print(f"[Gmail] Premier lancement — historyId sauvegardé.")
        print(f"[Gmail] Les prochains nouveaux mails seront détectés automatiquement.")
        return []

    if last_history_id == current_history_id:
        return []

    try:
        history_result = service.users().history().list(
            userId="me",
            startHistoryId=last_history_id,
            historyTypes=["messageAdded"]
        ).execute()

        new_message_ids = set()
        for record in history_result.get("history", []):
            for msg_added in record.get("messagesAdded", []):
                new_message_ids.add(msg_added["message"]["id"])

        print(f"[Gmail] {len(new_message_ids)} nouveaux mails détectés")
        for msg_id in new_message_ids:
            try:
                emails.append(fetch_email_by_id(service, msg_id))
            except Exception as e:
                print(f"[Gmail] Erreur lecture {msg_id} : {e}")
    except Exception as e:
        print(f"[Gmail] Erreur history API : {e}")

    save_history_id(current_history_id)
    return emails




# ── Parsing IA via Gemini ─────────────────────────────────────

EVENT_TYPES = [
    "cinema",
    "meeting",
    "transport",
    "restaurant",
    "medical",
    "livraison",
    "concert_evenement",
    "autre_reservation",
]

SYSTEM_PROMPT = """Tu es un assistant qui analyse des emails et extrait les événements importants.

Réponds UNIQUEMENT en JSON valide :
{
  "type": "cinema"|"meeting"|"transport"|"restaurant"|"medical"|"livraison"|"concert_evenement"|"autre_reservation"|"rien",
  "titre": "titre court de l'événement",
  "date": "YYYY-MM-DD ou null",
  "heure": "HH:MM ou null",
  "lieu": "adresse ou nom du lieu ou null",
  "lien": "lien zoom/meet/teams ou null",
  "organisateur": "nom ou null",
  "details": "résumé en 1 phrase",
  "action_suggérée": "ajouter_calendrier"|"reserver"|"rappel"|"rien"
}

Règles :
- type="rien" si c'est un mail publicitaire, newsletter, ou sans événement concret
- date au format YYYY-MM-DD si trouvée dans le mail
- action_suggérée="reserver" seulement si une réservation est encore possible
- action_suggérée="ajouter_calendrier" si c'est une confirmation déjà faite
"""

def parse_email_with_gemini(email: dict) -> Optional[dict]:
    """Analyse un mail avec Gemini et extrait l'événement."""
    import time

    if not GOOGLE_API_KEY:
        return parse_email_local(email)

    # Filtre rapide sujets sans intérêt
    subject_lower = email["subject"].lower()
    skip_keywords = ["tender", "cfdi", "report", "notification", "invoice",
                     "unsubscribe", "newsletter", "no-reply", "noreply",
                     "desinscri", "promo", "publicite"]
    if any(kw in subject_lower for kw in skip_keywords):
        return None

    prompt = f"""{SYSTEM_PROMPT}

Email à analyser :
De : {email['from']}
Sujet : {email['subject']}
Date : {email['date']}
Corps :
{email['body']}
"""

    for model in ["gemini-2.5-flash", "gemini-2.0-flash-lite", "gemini-2.0-flash"]:
        for attempt in range(3):
            try:
                time.sleep(1)
                r = _req.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GOOGLE_API_KEY}",
                    json={"contents": [{"parts": [{"text": prompt}]}]},
                    timeout=20
                )
                if r.status_code == 404:
                    break
                if r.status_code == 503:
                    print(f"[Gemini/{model}] 503 — retry {attempt+1}/3")
                    time.sleep(3 * (attempt + 1))
                    continue
                r.raise_for_status()
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                text = text.strip().replace("```json","").replace("```","").strip()
                result = json.loads(text)
                result["email_id"]      = email["id"]
                result["email_subject"] = email["subject"]
                result["email_from"]    = email["from"]
                return result
            except json.JSONDecodeError:
                return None
            except Exception as e:
                print(f"[Gemini/{model}] Erreur : {e}")
                break

    return parse_email_local(email)



# ── Fallback NLP local ────────────────────────────────────────

KEYWORDS = {
    "cinema":           ["cinéma", "séance", "film", "ugc", "mk2", "pathé", "gaumont", "billet"],
    "meeting":          ["zoom", "meet", "teams", "réunion", "meeting", "webinar", "appel"],
    "transport":        ["sncf", "eurostar", "tgv", "ter", "vol", "billet", "réservation train", "air france"],
    "restaurant":       ["thefork", "lafourchette", "réservation restaurant", "table", "dîner"],
    "medical":          ["doctolib", "rendez-vous médical", "consultation", "cabinet"],
    "livraison":        ["colis", "livraison", "amazon", "colissimo", "chronopost", "ups", "fedex"],
    "concert_evenement":["concert", "festival", "spectacle", "exposition", "ticketmaster", "fnac"],
}

def parse_email_local(email: dict) -> dict:
    """Parsing basique sans IA."""
    text = (email["subject"] + " " + email["body"]).lower()
    event_type = "rien"
    for t, keywords in KEYWORDS.items():
        if any(kw in text for kw in keywords):
            event_type = t
            break
    return {
        "type":             event_type,
        "titre":            email["subject"][:80],
        "date":             None,
        "heure":            None,
        "lieu":             None,
        "lien":             None,
        "organisateur":     None,
        "details":          f"Mail de {email['from']}",
        "action_suggérée":  "ajouter_calendrier" if event_type != "rien" else "rien",
        "email_id":         email["id"],
        "email_subject":    email["subject"],
        "email_from":       email["from"],
    }


# ── Pipeline complet ──────────────────────────────────────────

def run(save_to: str = "parsed_events.json") -> list:
    """
    Lance le pipeline complet :
    1. Auth Gmail
    2. Fetch mails récents
    3. Parse chaque mail avec Gemini
    4. Retourne les événements détectés
    """
    print("\n[SmartRDV] Analyse des mails Gmail...")
    print("=" * 50)

    service = get_gmail_service()
    emails  = fetch_recent_emails(service)

    events = []
    for i, email in enumerate(emails):
        print(f"[{i+1}/{len(emails)}] {email['subject'][:60]}")
        result = parse_email_with_gemini(email)
        if result and result.get("type") != "rien":
            events.append(result)
            print(f"  → {result['type'].upper()} : {result['titre']}")

    # Sauvegarde
    with open(save_to, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*50}")
    print(f"✅ {len(events)} événements détectés sur {len(emails)} mails")
    for e in events:
        date_str = e.get("date") or "date inconnue"
        print(f"  · [{e['type']}] {e['titre']} — {date_str}")
    print(f"→ Sauvegardé dans {save_to}")
    print("=" * 50)

    return events


# ── Endpoint FastAPI (à intégrer dans main.py) ────────────────
# 
# @app.get("/gmail/scan")
# def gmail_scan():
#     from gmail_parser import run
#     events = run()
#     return {"events": events, "count": len(events)}
#


if __name__ == "__main__":
    # Charge la clé depuis .env si présente
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
    events = run()

    if events:
        print("\nProchaine étape : intégration Google Calendar")