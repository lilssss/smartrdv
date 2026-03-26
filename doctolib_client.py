"""
Doctolib API Client — Reverse-engineered (hackathon use only)
=============================================================

WARNING: This module uses Doctolib's private, undocumented API.
  - Usage violates Doctolib's Terms of Service.
  - For demonstration / hackathon purposes only.
  - Never use in production or for mass automated booking.
  - Production path: Doctolib Partner API program.

Architecture
------------
  DoctolibSession    → manages cookies, headers, session state
  DoctolibSearcher   → finds practitioners by specialty + location
  DoctolibSlotFetcher→ fetches available appointment slots
  DoctolibAdapter    → converts Doctolib slots → our Slot dataclass
  DoctolibOptimizer  → full pipeline: search → fetch → score → recommend

Quick start
-----------
  python doctolib_client.py

  Or import and use programmatically:
    from doctolib_client import DoctolibOptimizer
    result = DoctolibOptimizer().find_best(
        specialty="dermatologue",
        location="Paris",
        user_prefs=UserPreferences(preferred_hours=(9, 12), max_travel_minutes=20)
    )
"""

from __future__ import annotations

import time
import random
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from typing import Optional

import requests

# Import our scoring engine (must be in the same directory)
try:
    from scheduler_optimizer import Slot, Weights, ScoringEngine, Optimizer, print_report
except ImportError:
    raise ImportError(
        "scheduler_optimizer.py not found. "
        "Make sure both files are in the same directory."
    )


# ---------------------------------------------------------------------------
# User preferences
# ---------------------------------------------------------------------------

@dataclass
class UserPreferences:
    """
    User-level constraints and preferences fed into the scoring engine.

    preferred_hours     : tuple (start_hour, end_hour) for preferred time window
    preferred_day       : jour préféré 0=lundi..6=dimanche, None=indifférent
    max_travel_minutes  : beyond this threshold, travel cost rises steeply
    busy_days           : list of dates already packed with events
    weights             : override default scoring weights
    """
    preferred_hours:    tuple[int, int] = (9, 13)
    preferred_day:      int = None   # 0=lundi 1=mardi ... 4=vendredi 5=sam 6=dim
    max_travel_minutes: int = 30
    busy_days:          list[str] = field(default_factory=list)
    weights:            Optional[Weights] = None


# ---------------------------------------------------------------------------
# Session manager
# ---------------------------------------------------------------------------

class DoctolibSession:
    """
    Manages an HTTP session that mimics a real browser visiting Doctolib.

    Key points:
    - Sets a realistic User-Agent to avoid bot detection
    - Visits the homepage first to get a valid session cookie + CSRF token
    - Adds a small random delay between requests (polite scraping)
    """

    BASE_URL = "https://www.doctolib.fr"

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.doctolib.fr/",
        "Connection": "keep-alive",
    }

    def __init__(self, min_delay: float = 0.8, max_delay: float = 2.0):
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._csrf_token: Optional[str] = None

    def _polite_delay(self):
        """Random delay between requests to avoid rate limiting."""
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    def init(self) -> bool:
        """
        Visit homepage to establish session cookies and extract CSRF token.
        Returns True if successful.
        """
        try:
            resp = self.session.get(self.BASE_URL, timeout=10)
            resp.raise_for_status()
            # CSRF token is in a meta tag: <meta name="csrf-token" content="...">
            if 'csrf-token' in resp.text:
                start = resp.text.find('name="csrf-token"')
                chunk = resp.text[start:start+200]
                token_start = chunk.find('content="') + 9
                token_end = chunk.find('"', token_start)
                self._csrf_token = chunk[token_start:token_end]
                self.session.headers["X-CSRF-Token"] = self._csrf_token
            return True
        except requests.RequestException as e:
            print(f"[DoctolibSession] Failed to initialize session: {e}")
            return False

    def get(self, path: str, params: dict = None) -> dict:
        """Make a GET request and return parsed JSON."""
        self._polite_delay()
        url = f"{self.BASE_URL}{path}"
        resp = self.session.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Searcher — find practitioners
# ---------------------------------------------------------------------------

@dataclass
class Practitioner:
    """Minimal representation of a Doctolib practitioner."""
    id: int
    name: str
    specialty: str
    address: str
    city: str
    agenda_ids: list[int]
    practice_ids: list[int]
    visit_motive_ids: list[int]
    profile_url: str = ""

    def __repr__(self):
        return f"Dr. {self.name} — {self.specialty} — {self.address}, {self.city}"


class DoctolibSearcher:
    """
    Searches for practitioners on Doctolib by specialty and location.

    Endpoint: GET /search_results.json
    Key parameters:
      - query    : specialty slug (e.g. "dermatologue")
      - location : city name
      - page     : pagination
    """

    SEARCH_PATH = "/search_results.json"

    def __init__(self, session: DoctolibSession):
        self.session = session

    def search(
        self,
        specialty: str,
        location: str,
        page: int = 1,
        limit: int = 5,
    ) -> list[Practitioner]:
        """
        Returns a list of practitioners matching the query.
        Falls back to mock data if the request fails (useful for offline demo).
        """
        params = {
            "query": specialty,
            "location": location,
            "page": page,
            "limit": limit,
            "insurance_sector": "public",
        }

        try:
            data = self.session.get(self.SEARCH_PATH, params=params)
            return self._parse_results(data)
        except Exception as e:
            print(f"[DoctolibSearcher] Live search failed ({e}), using mock data.")
            return self._mock_practitioners(specialty, location)

    def _parse_results(self, data: dict) -> list[Practitioner]:
        """
        Parse the Doctolib search JSON response.
        Structure: data['doctors'] → list of doctor objects
        """
        practitioners = []
        doctors = data.get("doctors", [])

        for doc in doctors:
            agendas = doc.get("agendas", [])
            agenda_ids = [a["id"] for a in agendas]
            practice_ids = list({a.get("practice_id") for a in agendas if a.get("practice_id")})
            visit_motive_ids = []
            for agenda in agendas:
                for motive in agenda.get("visit_motives", []):
                    visit_motive_ids.append(motive["id"])

            practitioners.append(Practitioner(
                id=doc.get("id", 0),
                name=doc.get("name_with_title", doc.get("name", "Inconnu")),
                specialty=doc.get("speciality", ""),
                address=doc.get("address", ""),
                city=doc.get("city", ""),
                agenda_ids=agenda_ids,
                practice_ids=practice_ids,
                visit_motive_ids=visit_motive_ids[:1],  # take first motive
                profile_url=f"https://www.doctolib.fr{doc.get('link', '')}",
            ))

        return practitioners

    @staticmethod
    def _mock_practitioners(specialty: str, location: str) -> list[Practitioner]:
        """Realistic mock data — used when live API is unavailable."""
        return [
            Practitioner(
                id=101, name="Martin Sophie", specialty=specialty,
                address="12 rue de la Paix", city=location,
                agenda_ids=[5001], practice_ids=[9001], visit_motive_ids=[2201],
                profile_url="https://www.doctolib.fr/dermatologue/paris/sophie-martin",
            ),
            Practitioner(
                id=102, name="Lefebvre Thomas", specialty=specialty,
                address="47 avenue des Ternes", city=location,
                agenda_ids=[5002], practice_ids=[9002], visit_motive_ids=[2202],
                profile_url="https://www.doctolib.fr/dermatologue/paris/thomas-lefebvre",
            ),
            Practitioner(
                id=103, name="Nguyen Anh", specialty=specialty,
                address="3 boulevard Voltaire", city=location,
                agenda_ids=[5003], practice_ids=[9003], visit_motive_ids=[2203],
                profile_url="https://www.doctolib.fr/dermatologue/paris/anh-nguyen",
            ),
        ]


# ---------------------------------------------------------------------------
# Slot fetcher — get available appointments
# ---------------------------------------------------------------------------

@dataclass
class RawSlot:
    """A raw available slot as returned by Doctolib."""
    start_date: str        # ISO 8601: "2026-03-18T09:00:00+01:00"
    end_date: str
    practitioner: Practitioner
    agenda_id: int
    visit_motive_id: int


class DoctolibSlotFetcher:
    """
    Fetches available appointment slots for a given practitioner.

    Endpoint: GET /availabilities.json
    Key parameters:
      - visit_motive_ids : type of consultation
      - agenda_ids       : practitioner's agendas
      - practice_ids     : clinic locations
      - start_date       : search window start (YYYY-MM-DD)
    """

    AVAILABILITY_PATH = "/availabilities.json"

    def __init__(self, session: DoctolibSession):
        self.session = session

    def fetch(
        self,
        practitioner: Practitioner,
        start_date: Optional[date] = None,
        days_ahead: int = 14,
    ) -> list[RawSlot]:
        """
        Fetches slots for the next `days_ahead` days.
        Falls back to mock data if request fails.
        """
        if start_date is None:
            start_date = date.today()

        params = {
            "visit_motive_ids": practitioner.visit_motive_ids[0] if practitioner.visit_motive_ids else "",
            "agenda_ids": "-".join(str(a) for a in practitioner.agenda_ids),
            "insurance_sector": "public",
            "practice_ids": practitioner.practice_ids[0] if practitioner.practice_ids else "",
            "start_date": start_date.strftime("%Y-%m-%d"),
        }

        try:
            data = self.session.get(self.AVAILABILITY_PATH, params=params)
            return self._parse_slots(data, practitioner)
        except Exception as e:
            print(f"[DoctolibSlotFetcher] Live fetch failed ({e}), using mock slots.")
            return self._mock_slots(practitioner, start_date, days_ahead)

    def _parse_slots(self, data: dict, practitioner: Practitioner) -> list[RawSlot]:
        """Parse Doctolib availability JSON → list of RawSlot."""
        slots = []
        for availability in data.get("availabilities", []):
            for slot in availability.get("slots", []):
                start = slot.get("start_date", "")
                end = slot.get("end_date", start)
                slots.append(RawSlot(
                    start_date=start,
                    end_date=end,
                    practitioner=practitioner,
                    agenda_id=slot.get("agenda_id", 0),
                    visit_motive_id=slot.get("visit_motive_id", 0),
                ))
        return slots

    @staticmethod
    def _mock_slots(
        practitioner: Practitioner,
        start_date: date,
        days_ahead: int,
    ) -> list[RawSlot]:
        """Generate realistic mock slots spread over the next N days."""
        slots = []
        times = [(9, 0), (9, 30), (10, 0), (11, 0), (14, 0), (14, 30), (15, 0), (16, 0)]
        random.seed(practitioner.id)  # deterministic per practitioner

        for day_offset in range(days_ahead):
            d = start_date + timedelta(days=day_offset)
            if d.weekday() >= 5:  # skip weekends
                continue
            for (h, m) in random.sample(times, k=random.randint(1, 4)):
                dt_str = f"{d.strftime('%Y-%m-%d')}T{h:02d}:{m:02d}:00+01:00"
                end_dt = datetime(d.year, d.month, d.day, h, m) + timedelta(minutes=20)
                end_str = f"{end_dt.strftime('%Y-%m-%dT%H:%M:%S')}+01:00"
                slots.append(RawSlot(
                    start_date=dt_str,
                    end_date=end_str,
                    practitioner=practitioner,
                    agenda_id=practitioner.agenda_ids[0],
                    visit_motive_id=practitioner.visit_motive_ids[0] if practitioner.visit_motive_ids else 0,
                ))

        slots.sort(key=lambda s: s.start_date)
        return slots


# ---------------------------------------------------------------------------
# Adapter — RawSlot → Slot (our scoring model)
# ---------------------------------------------------------------------------

class DoctolibAdapter:
    """
    Converts Doctolib RawSlot objects into our Slot dataclass,
    computing feature values based on user preferences and context.

    Feature heuristics:
      conflict      : always 0 (Doctolib only returns available slots)
      interruption  : high if slot is mid-afternoon (14-16h = prime work block)
      preference    : 1 if inside preferred_hours window, 0 otherwise
                      with smooth gradient near the edges
      travel        : normalized by max_travel_minutes (static estimate here,
                      in production: Google Maps Distance Matrix API)
      fatigue       : rises if the day already has 3+ slots in our list
    """

    def __init__(self, prefs: UserPreferences):
        self.prefs = prefs

    def _parse_date(self, start_date: str):
        """
        Parse les différents formats de date retournés par Doctolib :
        - ISO complet  : "2026-03-17T16:30:00+01:00"
        - Date+heure   : "2026-03-17 16:30"
        - Heure seule  : "16:30" (créneaux DOM du lazy loading)
        """
        from datetime import date, timedelta
        s = start_date.strip()
        # Heure seule ex: "16:30" ou "9h30"
        if len(s) <= 5 and (":" in s or "h" in s):
            s = s.replace("h", ":")
            parts = s.split(":")
            h = int(parts[0])
            m = int(parts[1]) if len(parts) > 1 and parts[1] else 0
            today = date.today()
            return datetime(today.year, today.month, today.day, h, m)
        # Format ISO ou date+heure
        try:
            return datetime.fromisoformat(s)
        except:
            # Dernier recours : retourne maintenant
            return datetime.now()

    def convert(self, raw_slots: list[RawSlot]) -> list[Slot]:
        day_density: dict[str, int] = {}
        for rs in raw_slots:
            day = rs.start_date[:10]
            day_density[day] = day_density.get(day, 0) + 1

        slots = []
        for rs in raw_slots:
            dt = self._parse_date(rs.start_date)
            day_key = rs.start_date[:10]

            # Si un jour précis est demandé et que le créneau n'est pas ce jour
            # → on le met en dernier en forçant conflict=1.0 (éliminatoire)
            wrong_day = (
                self.prefs.preferred_day is not None and
                dt.weekday() != self.prefs.preferred_day
            )

            slots.append(Slot(
                time=f"{rs.practitioner.name} — {dt.strftime('%a %d/%m %H:%M')}",
                conflict=1.0 if wrong_day else 0.0,
                interruption=self._interruption(dt.hour),
                preference=self._preference(dt.hour, dt.weekday()),
                travel=self._travel(rs.practitioner),
                fatigue=self._fatigue(day_key, day_density),
            ))
        return slots

    def _interruption(self, hour: int) -> float:
        """Peak work blocks: 10-12h and 14-16h are most disruptive."""
        if 10 <= hour <= 12:
            return 0.6
        if 14 <= hour <= 16:
            return 0.8
        if 9 <= hour <= 10:
            return 0.3
        return 0.2

    def _preference(self, hour: int, weekday: int = None) -> float:
        """
        Calcule la préférence : heure ET jour de la semaine.
        Si un jour préféré est défini et que le créneau n'est pas ce jour → pénalité forte.
        """
        # Pénalité jour : si vendredi demandé et c'est mardi → préférence = 0
        if self.prefs.preferred_day is not None and weekday is not None:
            if weekday != self.prefs.preferred_day:
                return 0.0  # mauvais jour → score maximal sur ce critère
        # Préférence horaire
        h_start, h_end = self.prefs.preferred_hours
        if h_start <= hour < h_end:
            return 1.0
        distance = min(abs(hour - h_start), abs(hour - h_end))
        return max(0.0, 1.0 - distance * 0.25)

    def _travel(self, practitioner: Practitioner) -> float:
        """
        Static travel estimate by arrondissement.
        In production: replace with Google Maps Distance Matrix API call.
        """
        travel_map = {
            "Paris": 0.2,
            "Boulogne-Billancourt": 0.4,
            "Levallois-Perret": 0.5,
            "Neuilly-sur-Seine": 0.4,
            "Vincennes": 0.6,
        }
        base = travel_map.get(practitioner.city, 0.5)
        return min(1.0, base)

    def _fatigue(self, day_key: str, density: dict) -> float:
        """More slots already on that day → higher fatigue penalty."""
        count = density.get(day_key, 0)
        if day_key in self.prefs.busy_days:
            return min(1.0, 0.4 + count * 0.2)
        return min(1.0, count * 0.15)


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

class DoctolibOptimizer:
    """
    End-to-end pipeline:
      1. Init session
      2. Search practitioners
      3. Fetch available slots
      4. Convert to scoring model
      5. Score and rank
      6. Return best slot

    Usage:
        optimizer = DoctolibOptimizer()
        result = optimizer.find_best("dermatologue", "Paris")
    """

    def __init__(self, prefs: Optional[UserPreferences] = None):
        self.prefs = prefs or UserPreferences()
        self.session = DoctolibSession()
        self.searcher = DoctolibSearcher(self.session)
        self.fetcher = DoctolibSlotFetcher(self.session)
        self.adapter = DoctolibAdapter(self.prefs)
        weights = self.prefs.weights or Weights(
            conflict=0.40,
            interruption=0.20,
            preference=0.25,
            travel=0.10,
            fatigue=0.05,
        )
        self.scoring_engine = ScoringEngine(weights)
        self.optimizer = Optimizer(self.scoring_engine)

    def find_best(
        self,
        specialty: str = "dermatologue",
        location: str = "Paris",
        max_practitioners: int = 3,
        top_n: int = 5,
    ):
        """
        Full pipeline. Returns the top_n ranked slots across all practitioners.
        """
        print(f"\nSearching for '{specialty}' in {location}...")

        # Step 1 — init session
        ok = self.session.init()
        if not ok:
            print("Session init failed — using mock data throughout.")

        # Step 2 — find practitioners
        practitioners = self.searcher.search(specialty, location)[:max_practitioners]
        print(f"Found {len(practitioners)} practitioners.")
        for p in practitioners:
            print(f"  · {p}")

        # Step 3 — fetch slots for each practitioner
        all_raw_slots = []
        for p in practitioners:
            print(f"\nFetching slots for {p.name}...")
            slots = self.fetcher.fetch(p)
            print(f"  → {len(slots)} available slots found.")
            all_raw_slots.extend(slots)

        if not all_raw_slots:
            print("No slots available.")
            return None

        # Step 4 — convert to scoring model
        scoring_slots = self.adapter.convert(all_raw_slots)

        # Step 5 — rank all slots
        ranked = self.optimizer.rank(scoring_slots)

        # Step 6 — print report
        print_report(ranked[:top_n])

        best = ranked[0]
        print(f"\nBest recommendation : {best.slot.time}")
        print(f"Cost score          : {best.total:.4f} (lower = better)\n")

        return best


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    prefs = UserPreferences(
        preferred_hours=(9, 12),     # prefer morning appointments
        max_travel_minutes=30,
        busy_days=[],
    )

    optimizer = DoctolibOptimizer(prefs=prefs)
    optimizer.find_best(
        specialty="dermatologue",
        location="Paris",
        max_practitioners=3,
        top_n=5,
    )