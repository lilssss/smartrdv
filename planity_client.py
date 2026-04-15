"""
Planity Client — Adapter + Scoring
=====================================
Convertit les données Planity (planity_slots.json) en objets
compatibles avec le ScoringEngine de SmartRDV.

Architecture calquée sur doctolib_client.py :
  PlanityPro       → équivalent Practitioner
  PlanityRawSlot   → équivalent RawSlot
  PlanityAdapter   → convertit en Slot (scoring model)
"""

from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from typing import Optional

try:
    from scheduler_optimizer import Slot, Weights, ScoringEngine, Optimizer
except ImportError:
    raise ImportError("scheduler_optimizer.py introuvable — même dossier requis.")


PLANITY_SLOTS_FILE = "planity_slots.json"

# ---------------------------------------------------------------------------
# Catégories disponibles sur Planity
# ---------------------------------------------------------------------------

PLANITY_CATEGORIES = [
    "coiffeur", "barbier", "manucure", "institut-de-beaute",
    "spa", "reflexologue", "massotherapeute", "sophrologue",
    "hypnotherapeute", "naturopathe", "coach-de-vie",
]

CATEGORY_LABELS = {
    "coiffeur":          "Coiffeurs",
    "barbier":           "Barbiers",
    "manucure":          "Manucure",
    "institut-de-beaute":"Instituts de beauté",
    "spa":               "Spa",
    "reflexologue":      "Réflexologues",
    "massotherapeute":   "Massothérapeutes",
    "sophrologue":       "Sophrologues",
    "hypnotherapeute":   "Hypnothérapeutes",
    "naturopathe":       "Naturopathes",
    "coach-de-vie":      "Coachs de vie",
}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class PlanityPro:
    """Professionnel Planity (salon, indépendant...)."""
    id:          int
    name:        str
    category:    str
    address:     str
    city:        str
    profile_url: str      = ""
    services:    list     = field(default_factory=list)
    agenda_ids:  list     = field(default_factory=list)

    def __repr__(self):
        return f"{self.name} — {self.category} — {self.address}, {self.city}"


@dataclass
class PlanityRawSlot:
    """Créneau brut tel que retourné par Planity."""
    start_date:  str
    end_date:    str
    pro:         PlanityPro
    service_id:  str = ""
    agenda_id:   str = ""


# ---------------------------------------------------------------------------
# Loader — lit planity_slots.json
# ---------------------------------------------------------------------------

def load_planity_data(path: str = PLANITY_SLOTS_FILE):
    """
    Charge planity_slots.json → (pros, raw_slots).
    Retourne ([], []) si le fichier est absent.
    """
    if not os.path.exists(path):
        return [], []

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    scraped_at = data.get("scraped_at", "?")
    print(f"[PlanityLoader] Chargement {path} (scraped: {scraped_at})")

    pros = []
    for p in data.get("practitioners", []):
        pros.append(PlanityPro(
            id=p.get("id", 0),
            name=p.get("name", "Inconnu"),
            category=p.get("category", ""),
            address=p.get("address", ""),
            city=p.get("city", ""),
            profile_url=p.get("profile_url", ""),
            services=p.get("services", []),
            agenda_ids=p.get("agenda_ids", []),
        ))

    pro_map = {p.id: p for p in pros}
    default_pro = pros[0] if pros else None

    raw_slots = []
    for s in data.get("slots", []):
        pro = pro_map.get(s.get("practitioner_id")) or default_pro
        if pro is None:
            continue
        raw_slots.append(PlanityRawSlot(
            start_date=s.get("start_date", ""),
            end_date=s.get("end_date", s.get("start_date", "")),
            pro=pro,
            service_id=str(s.get("service_id", "")),
            agenda_id=str(s.get("agenda_id", "")),
        ))

    print(f"[PlanityLoader] {len(pros)} pros, {len(raw_slots)} créneaux")
    return pros, raw_slots


# ---------------------------------------------------------------------------
# Adapter — PlanityRawSlot → Slot (scoring model)
# ---------------------------------------------------------------------------

@dataclass
class PlanityUserPreferences:
    preferred_hours:    tuple = (9, 18)
    preferred_day:      Optional[int] = None   # 0=lundi…6=dimanche
    busy_days:          list  = field(default_factory=list)


class PlanityAdapter:
    """
    Convertit PlanityRawSlot → Slot.
    Logique identique à DoctolibAdapter, adaptée au contexte bien-être.
    """

    def __init__(self, prefs: PlanityUserPreferences = None):
        self.prefs = prefs or PlanityUserPreferences()

    def _parse_date(self, s: str) -> datetime:
        s = s.strip()
        # Heure seule : "10:30" ou "10h30"
        if len(s) <= 5 and (":" in s or "h" in s):
            s = s.replace("h", ":")
            h, *rest = s.split(":")
            m = int(rest[0]) if rest else 0
            today = date.today()
            return datetime(today.year, today.month, today.day, int(h), m)
        try:
            return datetime.fromisoformat(s)
        except:
            return datetime.now()

    def convert(self, raw_slots: list[PlanityRawSlot]) -> list[Slot]:
        day_density: dict[str, int] = {}
        for rs in raw_slots:
            day = rs.start_date[:10]
            day_density[day] = day_density.get(day, 0) + 1

        slots = []
        for rs in raw_slots:
            if not rs.start_date:
                continue
            dt      = self._parse_date(rs.start_date)
            day_key = rs.start_date[:10]

            wrong_day = (
                self.prefs.preferred_day is not None and
                dt.weekday() != self.prefs.preferred_day
            )

            slots.append(Slot(
                time=f"{rs.pro.name} — {dt.strftime('%a %d/%m %H:%M')}",
                conflict=1.0 if wrong_day else 0.0,
                interruption=self._interruption(dt.hour),
                preference=self._preference(dt.hour, dt.weekday()),
                travel=self._travel(rs.pro),
                fatigue=self._fatigue(day_key, day_density),
            ))
        return slots

    def _interruption(self, hour: int) -> float:
        # Les créneaux bien-être sont souvent choisis hors heures de pointe
        if 12 <= hour <= 14:  # heure du déjeuner
            return 0.5
        if 18 <= hour <= 20:  # après le travail
            return 0.2
        return 0.3

    def _preference(self, hour: int, weekday: int = None) -> float:
        if self.prefs.preferred_day is not None and weekday is not None:
            if weekday != self.prefs.preferred_day:
                return 0.0
        h_start, h_end = self.prefs.preferred_hours
        if h_start <= hour < h_end:
            return 1.0
        distance = min(abs(hour - h_start), abs(hour - h_end))
        return max(0.0, 1.0 - distance * 0.25)

    def _travel(self, pro: PlanityPro) -> float:
        city_costs = {
            "Paris": 0.2, "Lyon": 0.2, "Marseille": 0.2,
            "Boulogne-Billancourt": 0.4, "Levallois-Perret": 0.5,
        }
        return city_costs.get(pro.city, 0.4)

    def _fatigue(self, day_key: str, density: dict) -> float:
        count = density.get(day_key, 0)
        if day_key in self.prefs.busy_days:
            return min(1.0, 0.4 + count * 0.2)
        return min(1.0, count * 0.15)


# ---------------------------------------------------------------------------
# Full pipeline (CLI / tests)
# ---------------------------------------------------------------------------

class PlanityOptimizer:
    def __init__(self, prefs: PlanityUserPreferences = None):
        self.prefs   = prefs or PlanityUserPreferences()
        self.adapter = PlanityAdapter(self.prefs)
        self.engine  = ScoringEngine(Weights())
        self.optim   = Optimizer(self.engine)

    def find_best(self, path: str = PLANITY_SLOTS_FILE, top_n: int = 5):
        pros, raw_slots = load_planity_data(path)
        if not raw_slots:
            print("[PlanityOptimizer] Aucun créneau disponible.")
            return None
        scoring_slots = self.adapter.convert(raw_slots)
        ranked = self.optim.rank(scoring_slots)
        from scheduler_optimizer import print_report
        print_report(ranked[:top_n])
        return ranked[0]


if __name__ == "__main__":
    opt = PlanityOptimizer()
    opt.find_best()
