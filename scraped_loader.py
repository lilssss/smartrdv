"""
Loader qui lit slots.json produit par doctolib_scraper.py
et le convertit en objets Practitioner + RawSlot.

Intègre dans doctolib_client.py en remplaçant
DoctolibSearcher.search() et DoctolibSlotFetcher.fetch()
par ScrapedDataLoader.load()
"""

import json
import os
from dataclasses import dataclass, field
from typing import Optional


SLOTS_FILE = "slots.json"


def load_scraped_data(path: str = SLOTS_FILE):
    """
    Charge slots.json et retourne (practitioners, raw_slots).
    Retourne ([], []) si le fichier n'existe pas.
    """
    if not os.path.exists(path):
        return [], []

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    scraped_at = data.get("scraped_at", "?")
    print(f"[Loader] Chargement de {path} (scraped: {scraped_at})")

    # Reconstruit des objets compatibles avec doctolib_client.py
    from doctolib_client import Practitioner, RawSlot

    practitioners = []
    for p in data.get("practitioners", []):
        practitioners.append(Practitioner(
            id=p.get("id", 0),
            name=p.get("name", "Inconnu"),
            specialty=p.get("specialty", ""),
            address=p.get("address", ""),
            city=p.get("city", ""),
            agenda_ids=p.get("agenda_ids", []),
            practice_ids=p.get("practice_ids", []),
            visit_motive_ids=p.get("visit_motive_ids", []),
            profile_url=p.get("profile_url", ""),
        ))

    # Associe chaque slot au premier praticien trouvé (simplification)
    # En production : matcher via agenda_id
    practitioner_map = {p.id: p for p in practitioners}
    default_prac = practitioners[0] if practitioners else None

    raw_slots = []
    for s in data.get("slots", []):
        agenda_id = s.get("agenda_id", 0)

        # Cherche le praticien qui possède cet agenda_id
        prac = default_prac
        for p in practitioners:
            if agenda_id in p.agenda_ids:
                prac = p
                break

        if prac is None:
            continue

        raw_slots.append(RawSlot(
            start_date=s.get("start_date", ""),
            end_date=s.get("end_date", s.get("start_date", "")),
            practitioner=prac,
            agenda_id=agenda_id,
            visit_motive_id=s.get("visit_motive_id", 0),
        ))

    print(f"[Loader] {len(practitioners)} praticiens, {len(raw_slots)} créneaux chargés")
    return practitioners, raw_slots
