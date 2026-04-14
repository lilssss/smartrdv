"""
Google Calendar — SmartRDV
===========================
- Vérifie les conflits avant d'ajouter un événement
- Ajoute dans Google Calendar si pas de conflit
- Génère un .ics pour Apple Calendar
"""

import os
from datetime import datetime, timedelta
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES           = ["https://www.googleapis.com/auth/calendar"]
CREDENTIALS_FILE = "gmail_credentials.json"
TOKEN_FILE       = "calendar_token.json"
CALENDAR_ID      = "primary"


# ── Auth ──────────────────────────────────────────────────────

def get_calendar_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("calendar", "v3", credentials=creds)


# ── Vérification des conflits ─────────────────────────────────

def check_conflicts(start_dt: datetime, end_dt: datetime) -> list:
    """
    Vérifie si un créneau est libre dans Google Calendar.
    Retourne la liste des événements en conflit (vide si libre).
    """
    service = get_calendar_service()

    events_result = service.events().list(
        calendarId  = CALENDAR_ID,
        timeMin     = start_dt.isoformat() + "Z",
        timeMax     = end_dt.isoformat() + "Z",
        singleEvents= True,
        orderBy     = "startTime",
    ).execute()

    conflicts = []
    for ev in events_result.get("items", []):
        ev_start = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date", "")
        ev_title = ev.get("summary", "Événement sans titre")
        conflicts.append({
            "title": ev_title,
            "start": ev_start,
        })

    return conflicts


# ── Ajout avec vérification ───────────────────────────────────

def add_event(event: dict) -> dict:
    """
    Vérifie les conflits puis ajoute l'événement dans Google Calendar.
    
    Retourne :
        {"success": True,  "link": "...", "conflicts": []}
        {"success": False, "conflicts": [...], "conflict_message": "..."}
        {"success": False, "error": "..."}
    """
    titre    = event.get("titre", "Événement SmartRDV")
    date_str = event.get("date")
    heure    = event.get("heure")
    lieu     = event.get("lieu", "")
    details  = event.get("details", "")
    lien     = event.get("lien", "")
    ev_type  = event.get("type", "")

    emoji = {
        "cinema": "🎬", "meeting": "📅", "transport": "🚂",
        "restaurant": "🍽️", "medical": "🏥", "livraison": "📦",
        "concert_evenement": "🎵",
    }.get(ev_type, "📌")

    if not date_str:
        return {"success": False, "error": "Date manquante — impossible d'ajouter au calendrier"}

    # Construit les datetimes
    try:
        if heure:
            start_dt = datetime.strptime(f"{date_str} {heure}", "%Y-%m-%d %H:%M")
        else:
            start_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=9)
        end_dt = start_dt + timedelta(hours=2)
    except ValueError as e:
        return {"success": False, "error": f"Format de date invalide : {e}"}

    # ── Vérifie les conflits ──────────────────────────────────
    try:
        conflicts = check_conflicts(start_dt, end_dt)
    except Exception as e:
        return {"success": False, "error": f"Impossible d'accéder au calendrier : {e}"}

    if conflicts:
        conflict_details = "\n".join([
            f"• {c['title']} ({c['start'][:16].replace('T',' ')})"
            for c in conflicts
        ])
        return {
            "success":          False,
            "conflicts":        conflicts,
            "conflict_message": (
                f"⚠️ Tu as déjà quelque chose de prévu à ce moment :\n\n"
                f"{conflict_details}\n\n"
                f"Veux-tu quand même ajouter *{titre}* ?"
            ),
            "event":            event,  # conservé pour forcer l'ajout
        }

    # ── Pas de conflit — ajoute l'événement ──────────────────
    description = "\n".join(filter(None, [details, f"Lien : {lien}" if lien else "", "Ajouté par SmartRDV"]))

    if heure:
        gcal_event = {
            "summary":     f"{emoji} {titre}",
            "location":    lieu,
            "description": description,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/Paris"},
            "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "Europe/Paris"},
            "reminders": {
                "useDefault": False,
                "overrides":  [
                    {"method": "popup", "minutes": 60},
                    {"method": "popup", "minutes": 15},
                ],
            },
        }
    else:
        gcal_event = {
            "summary":     f"{emoji} {titre}",
            "location":    lieu,
            "description": description,
            "start": {"date": date_str},
            "end":   {"date": date_str},
        }

    try:
        service = get_calendar_service()
        created = service.events().insert(calendarId=CALENDAR_ID, body=gcal_event).execute()
        link    = created.get("htmlLink", "")
        print(f"[Calendar] ✅ {gcal_event['summary']} ajouté → {link}")
        return {"success": True, "link": link, "conflicts": []}
    except Exception as e:
        return {"success": False, "error": str(e)}


def force_add_event(event: dict) -> dict:
    """Ajoute l'événement même s'il y a un conflit."""
    # Bypass la vérification — appelle directement l'insertion
    result = add_event(event)
    if result.get("conflicts"):
        # Recrée sans la vérification
        event_copy = dict(event)
        result2 = _insert_event(event_copy)
        return result2
    return result

def _insert_event(event: dict) -> dict:
    """Insère directement sans vérifier les conflits."""
    titre    = event.get("titre", "Événement")
    date_str = event.get("date")
    heure    = event.get("heure")
    lieu     = event.get("lieu", "")
    details  = event.get("details", "")
    ev_type  = event.get("type", "")
    emoji    = {"cinema":"🎬","meeting":"📅","transport":"🚂","restaurant":"🍽️","medical":"🏥"}.get(ev_type,"📌")

    if not date_str:
        return {"success": False, "error": "Date manquante"}
    try:
        if heure:
            start_dt = datetime.strptime(f"{date_str} {heure}", "%Y-%m-%d %H:%M")
        else:
            start_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=9)
        end_dt = start_dt + timedelta(hours=2)
        gcal_event = {
            "summary":     f"{emoji} {titre}",
            "location":    lieu,
            "description": details + "\nAjouté par SmartRDV (conflit ignoré)",
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/Paris"},
            "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "Europe/Paris"},
        }
        service = get_calendar_service()
        created = service.events().insert(calendarId=CALENDAR_ID, body=gcal_event).execute()
        return {"success": True, "link": created.get("htmlLink", ""), "conflicts": []}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── .ics pour Apple Calendar ──────────────────────────────────

def event_to_ics(event: dict) -> Optional[str]:
    """Génère un .ics compatible Apple Calendar."""
    titre    = event.get("titre", "Événement")
    date_str = event.get("date")
    heure    = event.get("heure")
    lieu     = event.get("lieu", "")
    details  = event.get("details", "")

    if not date_str:
        return None

    uid = f"smartrdv-{datetime.now().strftime('%Y%m%d%H%M%S')}@smartrdv"

    if heure:
        try:
            start_dt = datetime.strptime(f"{date_str} {heure}", "%Y-%m-%d %H:%M")
            end_dt   = start_dt + timedelta(hours=2)
            ics = f"""BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//SmartRDV//FR\nBEGIN:VEVENT\nUID:{uid}\nDTSTART;TZID=Europe/Paris:{start_dt.strftime('%Y%m%dT%H%M%S')}\nDTEND;TZID=Europe/Paris:{end_dt.strftime('%Y%m%dT%H%M%S')}\nSUMMARY:{titre}\nLOCATION:{lieu}\nDESCRIPTION:{details}\nBEGIN:VALARM\nTRIGGER:-PT1H\nACTION:DISPLAY\nDESCRIPTION:Rappel SmartRDV\nEND:VALARM\nEND:VEVENT\nEND:VCALENDAR"""
        except ValueError:
            return None
    else:
        ics = f"""BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//SmartRDV//FR\nBEGIN:VEVENT\nUID:{uid}\nDTSTART;VALUE=DATE:{date_str.replace('-','')}\nDTEND;VALUE=DATE:{date_str.replace('-','')}\nSUMMARY:{titre}\nLOCATION:{lieu}\nDESCRIPTION:{details}\nEND:VEVENT\nEND:VCALENDAR"""

    filename = f"smartrdv_{date_str}.ics"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(ics)
    return filename
