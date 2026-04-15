"""
Planity Crawler — Scroll Lazy Loading
======================================
Scrape les créneaux disponibles sur planity.com.
Même technique que doctolib_crawler.py :
  scroll chaque carte → Planity charge les dispos en AJAX → on intercepte.

Usage :
    python planity_crawler.py
    python planity_crawler.py coiffeur Paris
    python planity_crawler.py "nail bar" Lyon
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
CATEGORIES = {
    "1":  "coiffeur",
    "2":  "barbier",
    "3":  "manucure",
    "4":  "institut-de-beaute",
    "5":  "spa",
    "6":  "reflexologue",
    "7":  "massotherapeute",
    "8":  "sophrologue",
    "9":  "hypnotherapeute",
    "10": "naturopathe",
    "11": "coach-de-vie",
}

OUTPUT_FILE  = "planity_slots.json"
MAX_PROS     = 15
HEADLESS     = False


# ============================================================
# HELPERS
# ============================================================

def human_delay(min_s=1.2, max_s=2.5):
    time.sleep(random.uniform(min_s, max_s))


def slugify(text: str) -> str:
    """Paris → paris,  Île-de-France → ile-de-france"""
    replacements = {
        "é": "e", "è": "e", "ê": "e", "ë": "e",
        "à": "a", "â": "a", "ä": "a",
        "î": "i", "ï": "i",
        "ô": "o", "ö": "o",
        "û": "u", "ü": "u", "ù": "u",
        "ç": "c", " ": "-",
    }
    out = text.lower()
    for src, dst in replacements.items():
        out = out.replace(src, dst)
    return out


def parse_planity_slots(api_data: dict) -> list:
    """
    Parse la réponse JSON de l'API Planity (availabilities / agenda).
    Planity peut retourner plusieurs formats selon l'endpoint intercepté.
    """
    slots = []

    # Format 1 — availabilities list
    for avail in api_data.get("availabilities", []):
        for slot in avail.get("slots", []):
            if isinstance(slot, dict):
                slots.append({
                    "start_date":  slot.get("start_date") or slot.get("startDate", ""),
                    "end_date":    slot.get("end_date")   or slot.get("endDate", ""),
                    "service_id":  slot.get("service_id") or slot.get("serviceId", ""),
                    "agenda_id":   slot.get("agenda_id")  or slot.get("agendaId", ""),
                })
            elif isinstance(slot, str):
                slots.append({"start_date": slot, "end_date": slot, "service_id": "", "agenda_id": ""})

    # Format 2 — flat slots list
    if not slots:
        for slot in api_data.get("slots", []):
            if isinstance(slot, dict):
                slots.append({
                    "start_date":  slot.get("start_date") or slot.get("startDate") or slot.get("date", ""),
                    "end_date":    slot.get("end_date")   or slot.get("endDate", ""),
                    "service_id":  slot.get("service_id") or slot.get("serviceId", ""),
                    "agenda_id":   slot.get("agenda_id")  or slot.get("agendaId", ""),
                })

    # Format 3 — agenda days  {"2026-04-15": ["09:00", "09:30", ...], ...}
    if not slots:
        for day, times in api_data.items():
            if isinstance(times, list) and len(day) == 10 and day[4] == "-":
                for t in times:
                    if isinstance(t, str) and ":" in t:
                        slots.append({
                            "start_date": f"{day}T{t}:00",
                            "end_date":   "",
                            "service_id": "",
                            "agenda_id":  "",
                        })

    return slots


# ============================================================
# SCRAPER PRINCIPAL
# ============================================================

def scrape(category: str, location: str) -> dict:
    cat_slug = slugify(category)

    print(f"\n[Planity] Catégorie : {category}")
    print(f"[Planity] Ville     : {location}")
    print(f"[Planity] Headless  : {HEADLESS}\n")

    all_pros   = []
    all_slots  = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
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

        # ── URL directe Planity : /categorie/ville-cp ─────────
        # Format réel : planity.com/coiffeur/paris-75
        CITY_SLUGS = {
            "paris":       "paris-75",
            "lyon":        "lyon-69",
            "marseille":   "marseille-13",
            "bordeaux":    "bordeaux-33",
            "toulouse":    "toulouse-31",
            "nice":        "nice-06",
            "nantes":      "nantes-44",
            "strasbourg":  "strasbourg-67",
            "montpellier": "montpellier-34",
            "rennes":      "rennes-35",
            "lille":       "lille-59",
        }

        loc_slug   = slugify(location)
        city_slug  = CITY_SLUGS.get(loc_slug, loc_slug)
        search_url = f"https://www.planity.com/{cat_slug}/{city_slug}"

        print(f"[Planity] URL directe : {search_url}")
        page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
        human_delay(2, 3)

        # ── Accepte les cookies ───────────────────────────────
        for selector in [
            'button[id*="accept"]',
            'button[class*="accept"]',
            'button:has-text("Accepter")',
            'button:has-text("Tout accepter")',
            "button:has-text(\"J'accepte\")",
            '[data-testid*="accept"]',
            '#didomi-notice-agree-button',
        ]:
            try:
                btn = page.locator(selector).first
                if btn.is_visible(timeout=2000):
                    btn.click()
                    print(f"[Planity] Cookies acceptés")
                    human_delay(0.8, 1.5)
                    break
            except:
                pass

        # Si l'URL directe ne marche pas → fallback formulaire
        if cat_slug not in page.url:
            print("[Planity] URL directe échouée → fallback formulaire")
            page.goto("https://www.planity.com", wait_until="domcontentloaded", timeout=20000)
            human_delay(2, 3)
            # Clic sur la catégorie dans le menu si disponible
            for menu_sel in [
                f'a:has-text("{category}")',
                f'nav a[href*="{cat_slug}"]',
                f'a[class*="category"][href*="{cat_slug}"]',
            ]:
                try:
                    el = page.locator(menu_sel).first
                    if el.is_visible(timeout=1500):
                        el.click()
                        human_delay(1, 2)
                        break
                except:
                    pass

        human_delay(1, 2)
        print(f"[Planity] URL actuelle : {page.url}")

        # ── Attend les cartes pro ──────────────────────────────
        print("[Planity] Attente des cartes professionnels...")
        # Les vraies cartes salon ont des boutons de créneaux avec 'hasAvailabilities'
        # On scroll d'abord pour charger les cartes
        page.evaluate("window.scrollTo(0, 300)")
        human_delay(1, 2)

        CARD_SELECTORS = [
            # Cartes qui contiennent des boutons de disponibilité
            "div[class*='availabilities-module']",
            "div[class*='businessCard']",
            "div[class*='business-card']",
            "a[class*='business-card']",
            # Sélection par présence de boutons hasAvailabilities
            "div:has(button[class*='hasAvailabilities'])",
            "article:has(button[class*='hasAvailabilities'])",
            "li:has(button[class*='hasAvailabilities'])",
            "li[class*='result']",
        ]
        cards_locator = None
        for sel in CARD_SELECTORS:
            try:
                page.wait_for_selector(sel, timeout=6000)
                cards_locator = sel
                print(f"[Planity] Cartes trouvées avec : {sel}")
                break
            except:
                continue

        if not cards_locator:
            page.wait_for_timeout(5000)
            # Screenshot debug
            page.screenshot(path="debug_planity.png")
            print("[Planity] Aucun sélecteur trouvé — screenshot : debug_planity.png")
            browser.close()
            return save([], [], category, location)

        human_delay(1, 2)
        page.screenshot(path="debug_planity.png")
        print("[Planity] Screenshot : debug_planity.png")

        cards = page.locator(cards_locator).all()
        print(f"[Planity] {len(cards)} cartes trouvées")

        limit = min(MAX_PROS, len(cards))
        print(f"[Planity] Traitement de {limit} professionnels...\n")

        for i, card in enumerate(cards[:limit]):
            try:
                print(f"[Planity] ── Pro {i+1}/{limit}")

                # ── TECHNIQUE CLÉ : scroll → lazy loading ─────
                card.scroll_into_view_if_needed()
                human_delay(1.5, 2.5)

                # ── Nom ───────────────────────────────────────
                name = ""
                for name_sel in [
                    "[class*='business-name']",
                    "[class*='businessName']",
                    "[class*='name']",
                    "h2", "h3", "strong",
                ]:
                    try:
                        el = card.locator(name_sel).first
                        if el.is_visible(timeout=500):
                            candidate = el.inner_text().strip().split("\n")[0].strip()
                            if len(candidate) > 2:
                                name = candidate
                                break
                    except:
                        pass
                if not name:
                    name = f"Salon {i+1}"

                # ── Adresse — structure Oxygen-like ──────────
                address = ""

                # Essai 1 : div flex wrap (même structure que Doctolib Oxygen)
                try:
                    addr_div = card.locator('div.flex.flex-wrap').first
                    if addr_div.is_visible(timeout=500):
                        parts = [p.strip() for p in addr_div.all_inner_texts() if p.strip()]
                        if parts:
                            address = ", ".join(parts[:2])
                except:
                    pass

                # Essai 2 : sélecteurs sémantiques Planity
                if not address:
                    for addr_sel in [
                        "[class*='address']",
                        "[class*='location']",
                        "[class*='city']",
                        "address",
                        "p[class*='address']",
                    ]:
                        try:
                            el = card.locator(addr_sel).first
                            if el.is_visible(timeout=500):
                                address = el.inner_text().strip().replace("\n", ", ")
                                break
                        except:
                            pass

                # ── URL du profil ─────────────────────────────
                profile_url = ""
                try:
                    href = card.locator("a").first.get_attribute("href")
                    if href:
                        profile_url = (
                            "https://www.planity.com" + href
                            if href.startswith("/") else href
                        )
                        profile_url = profile_url.split("?")[0]
                except:
                    pass

                # ── Services disponibles ──────────────────────
                services = []
                try:
                    svc_els = card.locator(
                        "[class*='service'], [class*='category'], [class*='tag']"
                    ).all_inner_texts()
                    services = [s.strip() for s in svc_els if s.strip()][:5]
                except:
                    pass

                print(f"  Nom     : {name}")
                print(f"  Adresse : {address[:60] if address else 'N/A'}")
                print(f"  URL     : {profile_url[:60] if profile_url else 'N/A'}")

                # ── Créneaux DOM (boutons de slots visibles) ──
                dom_slots_text = []
                for slot_sel in [
                    'button[class*="hasAvailabilities"]',
                    'button[class*="availab"]',
                    'button[class*="slot"]',
                    'button[class*="timeslot"]',
                    'button[class*="time-slot"]',
                    '[data-testid*="slot"]',
                ]:
                    try:
                        texts = card.locator(slot_sel).all_inner_texts()
                        if texts:
                            dom_slots_text = [s.strip() for s in texts if s.strip()]
                            break
                    except:
                        pass
                print(f"  Créneaux DOM : {len(dom_slots_text)}")

                # ── Intercepte l'appel AJAX en cliquant "suivant" ──
                api_slots = []
                for next_sel in [
                    'button[aria-label*="suivant"]',
                    'button[aria-label*="next"]',
                    'button[class*="next"]',
                    'button[class*="arrow"]',
                    'svg[class*="chevron-right"]',
                    '[data-testid*="next"]',
                ]:
                    try:
                        btn = card.locator(next_sel).first
                        if btn.is_visible(timeout=1000):
                            with page.expect_response(
                                lambda r: any(
                                    k in r.url for k in
                                    ["availab", "slots", "agenda", "booking", "appointment"]
                                ),
                                timeout=6000,
                            ) as resp_info:
                                btn.click()
                            api_data  = resp_info.value.json()
                            api_slots = parse_planity_slots(api_data)
                            print(f"  Créneaux API : {len(api_slots)}")
                            break
                    except:
                        pass

                pro_id = i + 1

                # Sauvegarde créneaux DOM
                for slot_text in dom_slots_text:
                    all_slots.append({
                        "start_date":      slot_text,
                        "end_date":        slot_text,
                        "service_id":      "",
                        "agenda_id":       "",
                        "practitioner_id": pro_id,
                        "source":          "dom",
                    })

                # Sauvegarde créneaux API
                for slot in api_slots:
                    slot["practitioner_id"] = pro_id
                    slot["source"]          = "api"
                    all_slots.append(slot)

                total = len(dom_slots_text) + len(api_slots)
                all_pros.append({
                    "id":          pro_id,
                    "name":        name,
                    "category":    category,
                    "address":     address,
                    "city":        location,
                    "profile_url": profile_url,
                    "services":    services,
                    "slots_count": total,
                    "agenda_ids":  list({s["agenda_id"] for s in api_slots if s.get("agenda_id")}),
                })

                print(f"  Total créneaux : {total}")

            except Exception as e:
                print(f"  Erreur pro {i+1} : {e}")
                continue

        browser.close()

    return save(all_pros, all_slots, category, location)


# ============================================================
# SAVE
# ============================================================

def save(pros, slots, category, location) -> dict:
    output = {
        "scraped_at":  datetime.now().isoformat(),
        "platform":    "planity",
        "category":    category,
        "location":    location,
        "practitioners": pros,
        "slots":       slots,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*55}")
    print(f"  Planity — {category} / {location}")
    print(f"  Professionnels : {len(pros)}")
    print(f"  Créneaux       : {len(slots)}")
    if slots:
        print(f"  ✅ Succès !")
        for s in slots[:3]:
            print(f"    · {s.get('start_date','?')} ({s.get('source','?')})")
        print(f"  → Fichier sauvegardé : {OUTPUT_FILE}")
    else:
        print(f"  ⚠️  0 créneaux — salon peut-être complet ou sélecteurs à ajuster")
    print(f"{'='*55}\n")
    return output


# ============================================================
# MENU
# ============================================================

def menu():
    if len(sys.argv) >= 3:
        return sys.argv[1], sys.argv[2]

    print("\n╔══════════════════════════════════════════╗")
    print("║      PLANITY CRAWLER — SmartRDV          ║")
    print("╚══════════════════════════════════════════╝\n")
    labels = {
        "1":  "Coiffeurs",
        "2":  "Barbiers",
        "3":  "Manucure",
        "4":  "Instituts de beauté",
        "5":  "Spa",
        "6":  "Réflexologues",
        "7":  "Massothérapeutes",
        "8":  "Sophrologues",
        "9":  "Hypnothérapeutes",
        "10": "Naturopathes",
        "11": "Coachs de vie",
        "0":  "Autre (saisie libre)",
    }
    for k, v in labels.items():
        print(f"  {k:>2}. {v}")

    c = input("\nTon choix (numéro) : ").strip()
    cat = CATEGORIES.get(c) or (
        input("Catégorie : ").strip().lower() if c == "0" else "coiffeur"
    )
    loc = input("Ville : ").strip() or "Paris"
    return cat, loc


if __name__ == "__main__":
    category, location = menu()
    scrape(category, location)
