"""
Calendar Scheduler — SmartRDV
===============================
Enrichit le scoring des créneaux Doctolib avec :
  - conflict      : chevauchement avec Google Calendar
  - fatigue       : densité d'événements ce jour-là
  - interruption  : coupe une plage de travail
  - travel        : temps de trajet via Google Maps Distance Matrix
  - preference    : correspond aux horaires préférés

Usage dans main.py :
    from calendar_scheduler import CalendarScheduler
    scheduler = CalendarScheduler()
    scored_slots = scheduler.score_slots(raw_slots, user_prefs)
"""

import os
import requests
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

MAPS_API_KEY = os.environ.get("MAPS_API_KEY", "")

# ── Heures de travail (pour le critère interruption) ──────────
WORK_START = 9   # 9h
WORK_END   = 18  # 18h


# ── Récupération des événements Calendar ──────────────────────

def get_week_events(days_ahead: int = 14) -> list:
    """
    Récupère les événements Google Calendar des X prochains jours.
    Retourne une liste de dicts avec start, end, title, location.
    """
    try:
        from google_calendar import get_calendar_service
        service = get_calendar_service()

        now     = datetime.utcnow()
        time_min = now.isoformat() + "Z"
        time_max = (now + timedelta(days=days_ahead)).isoformat() + "Z"

        result = service.events().list(
            calendarId   = "primary",
            timeMin      = time_min,
            timeMax      = time_max,
            singleEvents = True,
            orderBy      = "startTime",
            maxResults   = 100,
        ).execute()

        events = []
        for ev in result.get("items", []):
            start_raw = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date", "")
            end_raw   = ev.get("end",   {}).get("dateTime") or ev.get("end",   {}).get("date", "")

            try:
                if "T" in start_raw:
                    # Nettoie la timezone pour comparaison naïve
                    start_dt = datetime.fromisoformat(start_raw[:19].replace("Z",""))
                    end_dt   = datetime.fromisoformat(end_raw[:19].replace("Z",""))
                else:
                    start_dt = datetime.strptime(start_raw, "%Y-%m-%d")
                    end_dt   = datetime.strptime(end_raw,   "%Y-%m-%d")
            except Exception:
                continue

            events.append({
                "title":    ev.get("summary", "Événement"),
                "start":    start_dt,
                "end":      end_dt,
                "location": ev.get("location", ""),
            })

        print(f"[Scheduler] {len(events)} événements Calendar chargés")
        return events

    except Exception as e:
        print(f"[Scheduler] Erreur Calendar : {e}")
        return []


# ── Google Maps — Distance Matrix ─────────────────────────────

_travel_cache = {}  # cache (origin, destination) → minutes

def get_travel_minutes(origin: str, destination: str) -> Optional[float]:
    """
    Retourne le temps de trajet en minutes entre deux adresses.
    Utilise un cache pour éviter les appels répétés.
    Retourne None si l'API est indisponible.
    """
    if not MAPS_API_KEY or not origin or not destination:
        return None

    # Normalise les adresses pour le cache
    key = (origin.strip().lower()[:50], destination.strip().lower()[:50])
    if key in _travel_cache:
        return _travel_cache[key]

    if origin.strip() == destination.strip():
        _travel_cache[key] = 0.0
        return 0.0

    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/distancematrix/json",
            params={
                "origins":      origin,
                "destinations": destination,
                "mode":         "transit",
                "language":     "fr",
                "key":          MAPS_API_KEY,
            },
            timeout=5,
        )
        data = r.json()
        element = data["rows"][0]["elements"][0]
        if element["status"] == "OK":
            minutes = element["duration"]["value"] / 60
            print(f"[Maps] {origin[:30]} → {destination[:30]} : {minutes:.0f} min")
            return minutes
        return None
    except Exception as e:
        print(f"[Maps] Erreur : {e}")
        _travel_cache[key] = None
        return None


# ── Scoring ───────────────────────────────────────────────────

class CalendarScheduler:

    def __init__(self, user_location: str = "Paris"):
        self.user_location  = user_location
        self.calendar_events = []
        self._loaded        = False

    def load(self):
        if not self._loaded:
            self.calendar_events = get_week_events()
            self._loaded = True

    def _get_day_events(self, dt: datetime) -> list:
        """Événements du même jour."""
        return [
            ev for ev in self.calendar_events
            if ev["start"].date() == dt.date()
        ]

    def _conflict_score(self, start: datetime, end: datetime) -> float:
        """
        0.0 = pas de conflit
        1.0 = chevauchement complet
        """
        for ev in self.calendar_events:
            if start < ev["end"] and end > ev["start"]:
                overlap_start = max(start, ev["start"])
                overlap_end   = min(end,   ev["end"])
                overlap_mins  = (overlap_end - overlap_start).total_seconds() / 60
                slot_mins     = (end - start).total_seconds() / 60
                return min(1.0, overlap_mins / max(slot_mins, 1))
        return 0.0

    def _get_conflicting_events(self, start: datetime, end: datetime) -> list:
        """
        Retourne la liste des événements Calendar en conflit avec ce créneau.
        Chaque événement : {title, start, end, location}
        """
        conflicts = []
        for ev in self.calendar_events:
            if start < ev["end"] and end > ev["start"]:
                conflicts.append({
                    "title":    ev.get("title", "Événement"),
                    "start":    ev["start"].strftime("%H:%M"),
                    "end":      ev["end"].strftime("%H:%M"),
                    "location": ev.get("location", ""),
                })
        return conflicts

    def _fatigue_score(self, dt: datetime) -> float:
        """
        Score basé sur la densité d'événements ce jour-là.
        0 événements → 0.0
        5+ événements → 1.0
        """
        day_events = self._get_day_events(dt)
        return min(1.0, len(day_events) / 5.0)

    def _interruption_score(self, start: datetime) -> float:
        """
        Pénalise les créneaux qui cassent une plage de travail.
        Milieu de matinée ou milieu d'après-midi → score élevé.
        """
        h = start.hour
        # Plages de travail concentrées : 9h-12h et 14h-17h
        if 10 <= h <= 11 or 14 <= h <= 16:
            return 0.8
        if 9 <= h <= 12 or 13 <= h <= 17:
            return 0.4
        # Tôt le matin, soir, midi → moins interruptif
        return 0.1

    def _preference_score(self, start: datetime, preferred_hours: list = None) -> float:
        """
        1.0 = créneau parfait (dans les horaires préférés)
        0.0 = créneau non désiré
        """
        if not preferred_hours or len(preferred_hours) < 2:
            # Pas de préférence → neutre
            return 0.5

        h = start.hour + start.minute / 60
        pref_start, pref_end = preferred_hours[0], preferred_hours[1]

        if pref_start <= h <= pref_end:
            return 1.0
        # Score dégressif : plus on est loin, moins c'est bon
        dist = min(abs(h - pref_start), abs(h - pref_end))
        return max(0.0, 1.0 - dist / 4.0)

    def _travel_score_from_minutes(self, travel_mins, max_travel_minutes=60.0) -> float:
        """Convertit des minutes de trajet en score [0,1]."""
        if travel_mins is None:
            return 0.2
        return min(1.0, travel_mins / max_travel_minutes)

    def _get_origin(self, start: datetime) -> str:
        """Retourne l'origine du trajet (dernier événement Calendar ou position user)."""
        preceding = None
        for ev in sorted(self.calendar_events, key=lambda e: e["start"]):
            if ev["end"] <= start and ev["location"]:
                preceding = ev
        return preceding["location"] if preceding else self.user_location

    def score_slot(
        self,
        slot_datetime_str: str,
        slot_location:     str = "",
        preferred_hours:   list = None,
        preferred_day:     int = None,
        duration_minutes:  int = 45,
    ) -> dict:
        """
        Calcule les 5 critères pour un créneau donné.
        Retourne un dict compatible avec scheduler_optimizer.Slot.

        slot_datetime_str : "2026-04-19T17:00:00" ou "2026-04-19 17:00"
        """
        # Parse la date
        try:
            if "T" in slot_datetime_str:
                start = datetime.fromisoformat(slot_datetime_str[:19])
            else:
                start = datetime.strptime(slot_datetime_str[:16], "%Y-%m-%d %H:%M")
        except Exception:
            # Format non reconnu → scores neutres
            return {
                "conflict": 0.0, "interruption": 0.3,
                "preference": 0.5, "travel": 0.2, "fatigue": 0.2
            }

        end = start + timedelta(minutes=duration_minutes)

        conflict     = self._conflict_score(start, end)
        fatigue      = self._fatigue_score(start)
        interruption = self._interruption_score(start)
        preference   = self._preference_score(start, preferred_hours)
        travel       = self._travel_score(start, slot_location)

        # Bonus jour préféré
        if preferred_day is not None and start.weekday() == preferred_day:
            preference = min(1.0, preference + 0.3)

        return {
            "conflict":     round(conflict,     3),
            "interruption": round(interruption, 3),
            "preference":   round(preference,   3),
            "travel":       round(travel,        3),
            "fatigue":      round(fatigue,       3),
        }

    def score_slots(
        self,
        raw_slots:       list,
        preferred_hours: list = None,
        preferred_day:   int  = None,
        preferred_date:  str  = None,  # "YYYY-MM-DD" — filtre strict par date
        location:        str  = "Paris",
    ) -> list:
        """
        Prend une liste de slots Doctolib bruts et retourne des objets
        scheduler_optimizer.Slot prêts à être rankés.
        Maps est calculé UNE SEULE FOIS par adresse unique (pas par slot).
        """
        from scheduler_optimizer import Slot

        self.user_location = location
        self.load()

        # ── Pré-calcul Maps : 1 appel par adresse unique ──────
        unique_addresses = set()
        for s in raw_slots:
            addr = s.get("address", "") or location
            if addr and addr != location:
                unique_addresses.add(addr)

        travel_cache = {}  # adresse → minutes
        origin = self.user_location

        # Limite à 15 adresses max pour éviter les timeouts
        import re as _re
        def is_useful_address(addr):
            """Accepte si l'adresse contient un code postal, arrondissement, ou rue."""
            if not addr or addr.lower() in (location.lower(), "paris", ""):
                return False
            # Code postal (75010, 92150...) ou arrondissement (Paris 10e, 10ème...)
            if _re.search(r'\b(7[0-9]|9[0-9])\d{3}\b', addr):
                return True
            if _re.search(r'\b\d+(e|er|ème|eme)\b', addr.lower()):
                return True
            # Contient un numéro de rue
            if _re.search(r'\b\d+\s+\w', addr):
                return True
            return False

        meaningful_addresses = [a for a in list(unique_addresses)[:15] if is_useful_address(a)]
        if meaningful_addresses:
            print(f"[Scheduler] Pré-calcul Maps pour {len(meaningful_addresses)} adresses...")
            for addr in meaningful_addresses:
                mins = get_travel_minutes(origin, addr)
                travel_cache[addr] = mins
        else:
            print(f"[Scheduler] Pas d'adresses précises — Maps ignoré")

        # ── Scoring ───────────────────────────────────────────
        scored = []
        self.slot_conflicts = {}  # label → [{title, start, end, location}]

        for s in raw_slots:
            slot_datetime = s.get("start_date", "")
            slot_location = s.get("address", "") or location

            # Parse la date (gère les timezones +02:00)
            try:
                if "T" in slot_datetime:
                    clean = slot_datetime[:19].replace("Z","")
                    start = datetime.fromisoformat(clean)
                else:
                    start = datetime.strptime(slot_datetime[:16], "%Y-%m-%d %H:%M")
            except Exception:
                continue

            end = start + timedelta(minutes=45)

            # Scores
            conflict     = self._conflict_score(start, end)
            fatigue      = self._fatigue_score(start)
            interruption = self._interruption_score(start)
            preference   = self._preference_score(start, preferred_hours)

            # Détails des conflits Calendar
            conflicting_events = self._get_conflicting_events(start, end)

            # Travel depuis le cache pré-calculé
            travel_mins  = travel_cache.get(slot_location)
            travel       = self._travel_score_from_minutes(travel_mins)

            # Filtre strict par date si précisée
            if preferred_date:
                try:
                    target_date = datetime.strptime(preferred_date, "%Y-%m-%d").date()
                    if start.date() != target_date:
                        continue  # ignore les créneaux qui ne sont pas ce jour
                    else:
                        preference = 1.0  # date parfaite
                except Exception:
                    pass
            elif preferred_day is not None:
                # Bonus fort pour le jour préféré
                if start.weekday() == preferred_day:
                    preference = 1.0  # jour parfait
                else:
                    days_diff = min(abs(start.weekday() - preferred_day),
                                   7 - abs(start.weekday() - preferred_day))
                    preference = max(0.0, preference - 0.15 * days_diff)

            scores = {
                "conflict":     round(conflict,     3),
                "interruption": round(interruption, 3),
                "preference":   round(preference,   3),
                "travel":       round(travel,        3),
                "fatigue":      round(fatigue,       3),
            }

            # Label lisible — fix encodage UTF-8
            try:
                prac_name = s.get("practitioner_name", s.get("name", "Médecin"))
                try:
                    prac_name = prac_name.encode('latin-1').decode('utf-8')
                except:
                    pass
                label = f"{prac_name} — {start.strftime('%a %d/%m %H:%M')}"
            except Exception:
                label = slot_datetime

            # Stocke les conflits Calendar pour ce créneau
            if conflicting_events:
                self.slot_conflicts[label] = conflicting_events

            try:
                scored.append(Slot(time=label, **scores))
            except Exception as e:
                print(f"[Scheduler] Slot invalide : {e}")

        return scored


# ── Test standalone ───────────────────────────────────────────

if __name__ == "__main__":
    scheduler = CalendarScheduler(user_location="Paris 1er")
    scheduler.load()

    test_slots = [
        {"start_date": "2026-04-21T09:00:00", "address": "15 Rue de Rivoli, Paris", "name": "Dr Martin"},
        {"start_date": "2026-04-21T14:00:00", "address": "52 Av des Champs-Élysées, Paris", "name": "Dr Dupont"},
        {"start_date": "2026-04-22T17:30:00", "address": "10 Rue de la Paix, Paris", "name": "Dr Leroy"},
        {"start_date": "2026-04-23T11:00:00", "address": "Paris 15e", "name": "Dr Bernard"},
    ]

    from scheduler_optimizer import Weights, ScoringEngine, Optimizer

    slots  = scheduler.score_slots(test_slots, preferred_hours=[17, 20], preferred_day=4)
    engine = ScoringEngine(Weights(conflict=0.40, interruption=0.20,
                                   preference=0.20, travel=0.10, fatigue=0.10))
    ranked = Optimizer(engine).rank(slots)

    print("\n=== RÉSULTATS ===")
    for i, d in enumerate(ranked):
        tag = " ⭐ RECOMMANDÉ" if i == 0 else ""
        print(f"\n#{i+1} {d.slot.time}{tag}")
        print(f"  Score total : {d.total:.3f}")
        for k, v in d.contributions.items():
            bar = "█" * int(v * 30)
            print(f"  {k:<14} {v:.3f}  {bar}")