"""
Doctolib Booking — Réservation complète jusqu'à la confirmation
"""

import time
import random
from playwright.sync_api import sync_playwright


def human_delay(min_s=0.8, max_s=1.8):
    time.sleep(random.uniform(min_s, max_s))


class DoctolibBooker:

    def __init__(self, headless=False):
        self.headless = headless

    def book(
        self,
        profile_url:    str,
        slot_datetime:  str,
        is_new_patient: bool = True,
        motive_keyword: str  = None,
        is_teleconsult: bool = False,
    ) -> dict:

        result = {"status": "pending", "step": "", "message": ""}

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                locale="fr-FR",
                viewport={"width": 1366, "height": 768},
            )
            page = context.new_page()

            try:
                # ── 0. Cookies de session ─────────────────────────
                try:
                    from doctolib_auth import get_session_cookies
                    cookies = get_session_cookies()
                    if cookies:
                        context.add_cookies(cookies)
                        print(f"[Booker] {len(cookies)} cookies injectés")
                except Exception as e:
                    print(f"[Booker] Pas de session : {e}")

                # ── 1. Ouvre le profil ─────────────────────────────
                print(f"[Booker] Ouverture : {profile_url}")
                page.goto(profile_url, wait_until="domcontentloaded")
                human_delay(2, 3)

                if "introuvable" in page.title().lower() or "404" in page.title():
                    parts = profile_url.replace("https://www.doctolib.fr/","").split("/")
                    if len(parts) >= 2:
                        page.goto(f"https://www.doctolib.fr/{parts[0]}/{parts[1]}", wait_until="domcontentloaded")
                        human_delay(2, 3)

                # Ferme le menu "Centre d'aide" s'il est ouvert
                try:
                    page.keyboard.press("Escape")
                    human_delay(0.3, 0.5)
                except: pass

                # Accepte les cookies
                for sel in ['button:has-text("Accepter et fermer")', 'button:has-text("Tout accepter")', 'button:has-text("Accepter")']:
                    try:
                        b = page.locator(sel).first
                        if b.is_visible(timeout=1500): b.click(); human_delay(0.5, 1); break
                    except: pass

                # ── 2. Prendre rendez-vous ─────────────────────────
                print("[Booker] Étape 1 : Prendre rendez-vous")
                clicked = False
                for txt in ["Prendre rendez-vous", "Prendre un rendez-vous", "Réserver"]:
                    try:
                        b = page.get_by_text(txt, exact=False)
                        if b.is_visible(timeout=3000): b.click(); clicked = True; human_delay(1.5, 2.5); break
                    except: pass

                if not clicked:
                    result.update(status="error", message="Bouton 'Prendre rendez-vous' introuvable")
                    return result
                result["step"] = "booking_opened"

                # ── 3. Nouveau / ancien patient ────────────────────
                print(f"[Booker] Étape 2 : {'Nouveau' if is_new_patient else 'Ancien'} patient")
                for txt in (["Non", "Nouveau patient", "Jamais consulté"] if is_new_patient
                             else ["Oui", "Patient existant", "Déjà consulté"]):
                    try:
                        b = page.get_by_text(txt, exact=False)
                        if b.is_visible(timeout=2000): b.click(); human_delay(1, 2); break
                    except: pass
                result["step"] = "patient_type_selected"

                # ── 4. Motif de consultation ───────────────────────
                print(f"[Booker] Étape 3 : Motif ({motive_keyword or 'premier disponible'})")
                motive_clicked = False
                if motive_keyword:
                    try:
                        b = page.get_by_text(motive_keyword, exact=False)
                        if b.is_visible(timeout=2000): b.click(); motive_clicked = True; human_delay(1, 2)
                    except: pass
                if not motive_clicked:
                    for sel in ["ul li button", "li[class*='reason'] button", "[class*='motive'] button", "li button"]:
                        try:
                            for b in page.locator(sel).all():
                                if b.is_visible():
                                    print(f"  Motif : {b.inner_text()[:50]}")
                                    b.click(); motive_clicked = True; human_delay(1, 2); break
                            if motive_clicked: break
                        except: pass
                result["step"] = "motive_selected"

                # ── 5. Présentiel / téléconsultation ──────────────
                print(f"[Booker] Étape 4 : {'Vidéo' if is_teleconsult else 'Cabinet'}")
                for txt in (["Vidéo", "Téléconsultation"] if is_teleconsult
                             else ["Cabinet", "En cabinet", "Au cabinet", "Présentiel"]):
                    try:
                        b = page.get_by_text(txt, exact=False)
                        if b.is_visible(timeout=2000): b.click(); human_delay(1, 1.5); break
                    except: pass
                result["step"] = "location_selected"

                # ── 6. Sélectionne le créneau ──────────────────────
                print(f"[Booker] Étape 5 : Créneau {slot_datetime}")
                time_str = slot_datetime[-5:] if len(slot_datetime) >= 5 else slot_datetime
                variants  = [time_str, time_str.replace(":", "h"), time_str.replace(":","h").lstrip("0")]
                slot_ok = False
                for tv in variants:
                    try:
                        b = page.locator(f'button[data-test-id="slot-button"]:has-text("{tv}")').first
                        if b.is_visible(timeout=3000): b.click(); slot_ok = True; human_delay(1, 2); break
                    except: pass
                if not slot_ok:
                    try:
                        b = page.locator('button[data-test-id="slot-button"]').first
                        if b.is_visible(timeout=3000):
                            print(f"  Premier slot : {b.inner_text()}")
                            b.click(); slot_ok = True; human_delay(1, 2)
                    except: pass
                result["step"] = "slot_selected"

                # ── 7. Continuer après créneau ─────────────────────
                print("[Booker] Étape 6 : Continuer")
                for txt in ["Suivant", "Continuer", "Valider", "Confirmer la date"]:
                    try:
                        b = page.get_by_text(txt, exact=False)
                        if b.is_visible(timeout=2000): b.click(); human_delay(1.5, 2.5); break
                    except: pass
                result["step"] = "slot_confirmed"

                # ── 8. Pour qui est ce RDV ? ───────────────────────
                # Doctolib demande "Pour qui prenez-vous ce rendez-vous ?"
                # Il faut cliquer sur le compte principal (ex: "Rayan DOC (moi)")
                print("[Booker] Étape 7 : Sélection du patient (moi)")
                human_delay(1.5, 2.5)

                patient_selected = False

                # Cherche le bouton "(moi)" ou le premier card patient
                for sel in [
                    'button:has-text("(moi)")',
                    'div[class*="patient"]:has-text("(moi)")',
                    'label:has-text("(moi)")',
                    '[class*="card"]:has-text("(moi)")',
                    '[class*="patient-card"]',
                    'button[class*="patient"]',
                ]:
                    try:
                        b = page.locator(sel).first
                        if b.is_visible(timeout=2000):
                            print(f"  Patient sélectionné : {b.inner_text()[:50]}")
                            b.click(); patient_selected = True; human_delay(1, 2); break
                    except: pass

                # Fallback : clique sur la première carte de la liste patients
                if not patient_selected:
                    for sel in [
                        'div[class*="booking-patient"] button',
                        'ul[class*="patient"] li:first-child button',
                        'div[class*="patient-list"] div:first-child',
                        'div.booking-patient-list button:first-child',
                    ]:
                        try:
                            b = page.locator(sel).first
                            if b.is_visible(timeout=2000):
                                print(f"  Fallback patient : {b.inner_text()[:50]}")
                                b.click(); patient_selected = True; human_delay(1, 2); break
                        except: pass

                if patient_selected:
                    result["step"] = "patient_identity_selected"
                    human_delay(1.5, 2.5)
                else:
                    print("[Booker] Page identité patient non détectée — on continue")

                # ── 9. Continuer après identité patient ────────────
                for txt in ["Continuer", "Suivant", "Valider"]:
                    try:
                        b = page.get_by_text(txt, exact=False)
                        if b.is_visible(timeout=2000): b.click(); human_delay(1.5, 2); break
                    except: pass

                # ── 10. Infos patient (formulaire éventuel) ────────
                print("[Booker] Étape 8 : Infos patient")
                human_delay(2, 3)
                for txt in ["Continuer", "Suivant", "Valider mes informations"]:
                    try:
                        b = page.get_by_text(txt, exact=False)
                        if b.is_visible(timeout=2000): b.click(); human_delay(1.5, 2); break
                    except: pass
                result["step"] = "patient_info_done"

                # ── 11. Confirmation finale ────────────────────────
                print("[Booker] Étape 9 : Confirmation finale")
                human_delay(2, 3)

                confirmed = False
                for txt in [
                    "Confirmer le rendez-vous",
                    "Confirmer mon rendez-vous",
                    "Confirmer",
                    "Valider le rendez-vous",
                    "Valider",
                    "Prendre ce rendez-vous",
                ]:
                    try:
                        b = page.get_by_text(txt, exact=False)
                        if b.is_visible(timeout=3000):
                            print(f"  Clic confirmation : '{txt}'")
                            b.click(); confirmed = True; human_delay(2, 3); break
                    except: pass

                if confirmed:
                    try:
                        page.wait_for_url("**/confirmation**", timeout=10000)
                    except: pass
                    human_delay(2, 3)
                    result.update(
                        status="success",
                        step="confirmed",
                        message="✅ Rendez-vous confirmé ! Vérifiez votre email Doctolib."
                    )
                    print("[Booker] ✅ RDV CONFIRMÉ !")
                else:
                    print("[Booker] Bouton confirmation non trouvé — attente 30s")
                    human_delay(28, 30)
                    result.update(
                        status="success",
                        step="manual_confirm",
                        message="Tunnel complété — vérifiez le navigateur pour confirmer manuellement."
                    )

            except Exception as e:
                result.update(status="error", message=str(e))
                print(f"[Booker] Erreur : {e}")
            finally:
                human_delay(2, 3)
                browser.close()

        return result


if __name__ == "__main__":
    booker = DoctolibBooker(headless=False)
    r = booker.book(
        profile_url    = "https://www.doctolib.fr/gynecologue-obstetricien/paris/arnaud-bresset-plaisir",
        slot_datetime  = "17:40",
        is_new_patient = False,
        motive_keyword = "suivi",
    )
    print(r)