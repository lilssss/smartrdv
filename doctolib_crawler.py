"""
Doctolib Crawler — Basé sur scroll lazy loading
=================================================
Technique : scroll chaque carte médecin → Doctolib charge
automatiquement availabilities.json via lazy loading.
Pas de connexion requise !

Crédits : technique adaptée depuis DoctolibScraper (scraper.py)

Usage :
    python doctolib_crawler.py
    python doctolib_crawler.py gynecologue Paris
"""

import json
import time
import random
import sys
from datetime import datetime
from playwright.sync_api import sync_playwright

# ============================================================
# CONFIG
# ============================================================
SPECIALTIES = {
    "1":"dermatologue",       "2":"medecin-generaliste",
    "3":"cardiologue",        "4":"ophtalmologue",
    "5":"pediatre",           "6":"gynecologue",
    "7":"psychiatre",         "8":"orthopediste",
    "9":"dentiste",           "10":"kinesitherapeute",
    "11":"radiologue",        "12":"endocrinologue",
}

OUTPUT_FILE       = "slots.json"
MAX_DOCTORS       = 15     # nb max de cartes à scraper
HEADLESS          = False  # False = navigateur visible
LIMIT             = False  # True = s'arrête à 5 médecins (debug)

# ============================================================
# HELPERS
# ============================================================
def human_delay(min_s=1.2, max_s=2.5):
    """Délai aléatoire pour simuler un comportement humain."""
    time.sleep(random.uniform(min_s, max_s))


def parse_availabilities_json(api_data: dict) -> list:
    """
    Parse la réponse de availabilities.json.
    Extrait tous les créneaux avec leur date/heure complète.
    """
    slots = []
    for day in api_data.get("availabilities", []):
        for slot in day.get("slots", []):
            if isinstance(slot, dict):
                slots.append({
                    "start_date":      slot.get("start_date", ""),
                    "end_date":        slot.get("end_date", slot.get("start_date", "")),
                    "agenda_id":       slot.get("agenda_id", 0),
                    "visit_motive_id": slot.get("visit_motive_id", 0),
                })
            elif isinstance(slot, str):
                slots.append({
                    "start_date":      slot,
                    "end_date":        slot,
                    "agenda_id":       0,
                    "visit_motive_id": 0,
                })
        # Créneaux de substitution
        sub = day.get("substitution")
        if sub and isinstance(sub, dict):
            for sub_name, sub_info in sub.items():
                for slot in sub_info.get("slots", []):
                    if isinstance(slot, dict):
                        slots.append({
                            "start_date":      slot.get("start_date", ""),
                            "end_date":        slot.get("end_date", ""),
                            "agenda_id":       slot.get("agenda_id", 0),
                            "visit_motive_id": slot.get("visit_motive_id", 0),
                        })
    return slots


# ============================================================
# SCRAPER PRINCIPAL
# ============================================================
def scrape(specialty: str, location: str) -> dict:
    location_slug = (
        location.lower()
        .replace(" ", "-").replace("é","e").replace("è","e").replace("ê","e")
    )

    # URL de recherche Doctolib
    search_url = (
        f"https://www.doctolib.fr/search"
        f"?keyword={specialty}&location={location_slug}"
    )

    print(f"\n[Crawler] Spécialité : {specialty}")
    print(f"[Crawler] Ville      : {location}")
    print(f"[Crawler] URL        : {search_url}")
    print(f"[Crawler] Headless   : {HEADLESS}\n")

    all_practitioners = []
    all_slots         = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="fr-FR",
        )
        page = context.new_page()

        # ── Ouvre la page de recherche ────────────────────────
        print(f"[Crawler] Ouverture de la page de recherche...")
        page.goto(search_url)

        # ── Accepte les cookies ───────────────────────────────
        try:
            cookie_btn = page.locator("button#didomi-notice-agree-button")
            cookie_btn.wait_for(state="visible", timeout=5000)
            cookie_btn.click()
            print("[Crawler] Cookies acceptés")
            human_delay(1, 2)
        except:
            # Essaie d'autres sélecteurs de cookies
            for txt in ["Accepter", "ACCEPTER", "Tout accepter"]:
                try:
                    btn = page.get_by_text(txt, exact=True)
                    if btn.is_visible():
                        btn.click()
                        print(f"[Crawler] Cookies acceptés ({txt})")
                        human_delay(0.8, 1.5)
                        break
                except:
                    pass

        # ── Attend les cartes médecins ─────────────────────────
        print("[Crawler] Attente des cartes médecins...")
        try:
            page.wait_for_selector("div.dl-card:visible", timeout=15000)
            human_delay(2, 3)
        except:
            print("[Crawler] Sélecteur dl-card non trouvé — essai alternatif...")
            try:
                page.wait_for_selector("[data-test='search-result']:visible", timeout=10000)
            except:
                page.wait_for_timeout(6000)

        # Screenshot debug
        page.screenshot(path="debug_search.png")
        print("[Crawler] Screenshot : debug_search.png")

        # ── Récupère toutes les cartes visibles ───────────────
        cards = page.locator("div.dl-card:visible").all()
        print(f"[Crawler] {len(cards)} cartes trouvées")

        if not cards:
            # Fallback : essaie d'autres sélecteurs
            cards = page.locator("[data-test='search-result']:visible").all()
            print(f"[Crawler] Fallback : {len(cards)} cartes")

        limit = min(MAX_DOCTORS, len(cards)) if not LIMIT else min(5, len(cards))
        print(f"[Crawler] Traitement de {limit} médecins...\n")

        for i, card in enumerate(cards[:limit]):
            try:
                print(f"[Crawler] ── Médecin {i+1}/{limit}")

                # ── TECHNIQUE CLÉ : scroll → lazy loading ────
                card.scroll_into_view_if_needed()
                human_delay(1.5, 2.5)  # Doctolib charge le calendrier

                # Récupère le nom — sélecteurs Doctolib 2024/2025
                name = ""
                for name_selector in [
                    "[data-test='doctor-name']",
                    "[class*='dl-doctor-name']",
                    "[class*='doctor-name']",
                    "[class*='practitioner-name']",
                    "h2[class*='name']",
                    "h3[class*='name']",
                    "h2[class*='title']",
                    "h3[class*='title']",
                    "h2", "h3",
                    "[class*='name']",
                ]:
                    try:
                        name_el = card.locator(name_selector).first
                        if name_el.is_visible(timeout=500):
                            candidate = name_el.inner_text().strip().split("\n")[0].strip()
                            if len(candidate) > 4 and not any(w in candidate.lower() for w in ["prendre", "voir", "disponible", "rdv"]):
                                name = candidate
                                break
                    except:
                        pass

                # Fallback : premier lien qui ressemble à un nom
                if not name:
                    try:
                        for link in card.locator("a").all()[:5]:
                            txt = link.inner_text().strip().split("\n")[0].strip()
                            if len(txt) > 4 and any(w in txt for w in ["Dr", "Mme", "M.", "Docteur"]):
                                name = txt
                                break
                    except:
                        pass

                if not name:
                    name = f"Praticien {i+1}"

                # Récupère l'adresse
                address = ""
                for addr_sel in ["[data-test='address']", "[class*='address']", "address"]:
                    try:
                        addr_el = card.locator(addr_sel).first
                        if addr_el.is_visible():
                            address = addr_el.inner_text().strip().replace("\n", " ")
                            break
                    except:
                        pass

                # Récupère le lien du profil
                profile_url = ""
                try:
                    href = card.locator("a").first.get_attribute("href")
                    if href:
                        profile_url = "https://www.doctolib.fr" + href if href.startswith("/") else href
                        profile_url = profile_url.split("?")[0]
                except:
                    pass

                print(f"  Nom     : {name}")
                print(f"  Adresse : {address[:50] if address else 'N/A'}")

                # ── Créneaux visibles dans le DOM ─────────────
                dom_slots_text = card.locator(
                    'button[data-test-id="slot-button"]'
                ).all_inner_texts()
                dom_slots = [s.strip() for s in dom_slots_text if s.strip()]
                print(f"  Créneaux DOM : {len(dom_slots)}")

                # ── Clic sur "Suivant" → intercepte availabilities.json ──
                api_slots = []
                next_btn = card.locator(
                    'button:has(svg[data-icon-name="regular/chevron-right"])'
                ).first

                if next_btn.is_visible():
                    try:
                        with page.expect_response(
                            lambda r: "availabilities.json" in r.url,
                            timeout=6000
                        ) as response_info:
                            next_btn.click()

                        api_data  = response_info.value.json()
                        api_slots = parse_availabilities_json(api_data)
                        print(f"  Créneaux API : {len(api_slots)}")
                    except Exception as e:
                        print(f"  API timeout ou pas de réponse")
                else:
                    print(f"  Pas de bouton 'Suivant' (aucune dispo future visible)")

                # ── Convertit les créneaux DOM en format unifié ──
                practitioner_id = i + 1
                for slot_text in dom_slots:
                    all_slots.append({
                        "start_date":      slot_text,
                        "end_date":        slot_text,
                        "agenda_id":       0,
                        "visit_motive_id": 0,
                        "practitioner_id": practitioner_id,
                        "source":          "dom",
                    })

                for slot in api_slots:
                    slot["practitioner_id"] = practitioner_id
                    slot["source"]          = "api"
                    all_slots.append(slot)

                total_slots = len(dom_slots) + len(api_slots)
                all_practitioners.append({
                    "id":               practitioner_id,
                    "name":             name,
                    "specialty":        specialty,
                    "address":          address,
                    "city":             location,
                    "profile_url":      profile_url,
                    "agenda_ids":       list({s["agenda_id"] for s in api_slots if s.get("agenda_id")}),
                    "practice_ids":     [],
                    "visit_motive_ids": list({s["visit_motive_id"] for s in api_slots if s.get("visit_motive_id")}),
                    "slots_count":      total_slots,
                })

                print(f"  Total créneaux : {total_slots}")

            except Exception as e:
                print(f"  Erreur : {e}")
                continue

        browser.close()

    return save(all_practitioners, all_slots, specialty, location)


# ============================================================
# SAVE
# ============================================================
def save(practitioners, slots, specialty, location):
    output = {
        "scraped_at":    datetime.now().isoformat(),
        "specialty":     specialty,
        "location":      location,
        "practitioners": practitioners,
        "slots":         slots,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*55}")
    print(f"  {specialty} — {location}")
    print(f"  Praticiens  : {len(practitioners)}")
    print(f"  Créneaux    : {len(slots)}")
    if slots:
        print(f"  ✅ Succès !")
        # Affiche quelques créneaux
        for s in slots[:3]:
            print(f"    · {s.get('start_date','?')} ({s.get('source','?')})")
        print(f"  → Relance uvicorn et recharge index.html")
    else:
        print(f"  ⚠️  0 créneaux — médecins peut-être complets")
    print(f"{'='*55}\n")
    return output


# ============================================================
# MENU
# ============================================================
def menu():
    if len(sys.argv) >= 3:
        return sys.argv[1], sys.argv[2]

    print("\n╔══════════════════════════════════════════╗")
    print("║   DOCTOLIB CRAWLER — Scroll Lazy Loading   ║")
    print("╚══════════════════════════════════════════╝\n")
    labels = {
        "1":"Dermatologue",       "2":"Médecin généraliste",
        "3":"Cardiologue",        "4":"Ophtalmologue",
        "5":"Pédiatre",           "6":"Gynécologue",
        "7":"Psychiatre",         "8":"Orthopédiste",
        "9":"Dentiste",           "10":"Kinésithérapeute",
        "11":"Radiologue",        "12":"Endocrinologue",
        "0":"Autre (saisie libre)",
    }
    for k, v in labels.items():
        print(f"  {k:>2}. {v}")

    c = input("\nTon choix (numéro) : ").strip()
    s = SPECIALTIES.get(c) or (
        input("Spécialité : ").strip().lower() if c == "0" else "dermatologue"
    )
    l = input("Ville : ").strip() or "Paris"
    return s, l


if __name__ == "__main__":
    specialty, location = menu()
    scrape(specialty, location)