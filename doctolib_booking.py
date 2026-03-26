"""
Doctolib Booking — Réservation automatique
==========================================
Navigue le tunnel de réservation Doctolib :
  1. Ouvre le profil du médecin
  2. Clique "Prendre rendez-vous"
  3. Répond aux questions (nouveau patient, motif, lieu)
  4. Sélectionne le créneau demandé
  5. S'arrête avant la confirmation finale (pour que l'utilisateur valide)

Usage :
    from doctolib_booking import DoctolibBooker
    booker = DoctolibBooker()
    booker.book(
        profile_url="https://www.doctolib.fr/gynecologue/paris/juliette-kinn",
        slot_datetime="2026-03-21 17:00",
        is_new_patient=True,
        motive_keyword="première consultation",
    )
"""

import time
import random
from playwright.sync_api import sync_playwright, Page


def human_delay(min_s=0.8, max_s=1.8):
    time.sleep(random.uniform(min_s, max_s))


class DoctolibBooker:
    """
    Automatise la navigation du tunnel de réservation Doctolib.
    S'arrête sur la page de confirmation pour que l'utilisateur valide.
    """

    def __init__(self, headless=False):
        self.headless = headless

    def book(
        self,
        profile_url: str,
        slot_datetime: str,       # ex: "2026-03-21 17:00"
        is_new_patient: bool = True,
        motive_keyword: str = None,  # ex: "première" → sélectionne le motif qui contient ce mot
        is_teleconsult: bool = False,
    ) -> dict:
        """
        Lance le tunnel de réservation.
        Retourne un dict avec le statut et l'étape atteinte.
        """
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
                # ── 0. Injecte les cookies de session ─────────
                try:
                    from doctolib_auth import get_session_cookies
                    session_cookies = get_session_cookies()
                    if session_cookies:
                        context.add_cookies(session_cookies)
                        print(f"[Booker] {len(session_cookies)} cookies de session injectés")
                except Exception as e:
                    print(f"[Booker] Pas de session Doctolib : {e}")

                # ── 1. Ouvre le profil ────────────────────────
                print(f"[Booker] Ouverture du profil : {profile_url}")
                page.goto(profile_url, wait_until="domcontentloaded")
                human_delay(2, 3)

                # Vérifie si la page existe (404 = URL incorrecte)
                if "introuvable" in page.title().lower() or "404" in page.title():
                    print(f"[Booker] Page introuvable — essai recherche Doctolib")
                    # Fallback : ouvre la page de recherche de la spécialité
                    parts = profile_url.replace("https://www.doctolib.fr/","").split("/")
                    if len(parts) >= 2:
                        search_url = f"https://www.doctolib.fr/{parts[0]}/{parts[1]}"
                        page.goto(search_url, wait_until="domcontentloaded")
                        human_delay(2, 3)
                        # Trouve et clique sur le premier médecin de la liste
                        try:
                            first_card = page.locator("div.dl-card:visible a").first
                            if first_card.is_visible(timeout=5000):
                                first_card.click()
                                human_delay(2, 3)
                        except:
                            pass

                # Cookies
                for txt in ["Accepter", "ACCEPTER", "button#didomi-notice-agree-button"]:
                    try:
                        if txt.startswith("button"):
                            page.locator(txt).click(timeout=2000)
                        else:
                            page.get_by_text(txt, exact=True).click(timeout=2000)
                        human_delay(0.5, 1)
                        break
                    except:
                        pass

                # ── 2. Clique "Prendre rendez-vous" ──────────
                print("[Booker] Étape 1 : Prendre rendez-vous")
                clicked = False
                for txt in ["Prendre rendez-vous", "Prendre un rendez-vous"]:
                    try:
                        btn = page.get_by_text(txt, exact=False)
                        if btn.is_visible(timeout=3000):
                            btn.click()
                            clicked = True
                            human_delay(1.5, 2.5)
                            result["step"] = "booking_opened"
                            break
                    except:
                        pass

                if not clicked:
                    result["status"]  = "error"
                    result["message"] = "Bouton 'Prendre rendez-vous' introuvable"
                    return result

                # ── 3. Nouveau / ancien patient ────────────────
                print(f"[Booker] Étape 2 : {'Nouveau' if is_new_patient else 'Ancien'} patient")
                patient_text = "Non" if is_new_patient else "Oui"
                for txt in [patient_text, "Nouveau patient", "Jamais consulté"]:
                    try:
                        btn = page.get_by_text(txt, exact=False)
                        if btn.is_visible(timeout=3000):
                            btn.click()
                            human_delay(1, 2)
                            result["step"] = "patient_type_selected"
                            break
                    except:
                        pass

                # ── 4. Motif de consultation ───────────────────
                print(f"[Booker] Étape 3 : Motif ({motive_keyword or 'premier disponible'})")
                motive_clicked = False

                if motive_keyword:
                    # Cherche un bouton qui contient le mot-clé
                    try:
                        btn = page.get_by_text(motive_keyword, exact=False)
                        if btn.is_visible(timeout=3000):
                            btn.click()
                            motive_clicked = True
                            human_delay(1, 2)
                    except:
                        pass

                if not motive_clicked:
                    # Prend le premier motif disponible
                    for sel in ["ul li button", "li[class*='reason'] button",
                                "[class*='motive'] button", "li button"]:
                        try:
                            btns = page.locator(sel).all()
                            for btn in btns:
                                if btn.is_visible():
                                    print(f"  Motif sélectionné : {btn.inner_text()[:50]}")
                                    btn.click()
                                    motive_clicked = True
                                    human_delay(1, 2)
                                    break
                            if motive_clicked:
                                break
                        except:
                            pass

                result["step"] = "motive_selected"

                # ── 5. Téléconsultation ou présentiel ─────────
                print(f"[Booker] Étape 4 : {'Téléconsultation' if is_teleconsult else 'Présentiel'}")
                consult_text = "Vidéo" if is_teleconsult else "Cabinet"
                for txt in [consult_text, "En cabinet", "Au cabinet"]:
                    try:
                        btn = page.get_by_text(txt, exact=False)
                        if btn.is_visible(timeout=2000):
                            btn.click()
                            human_delay(1, 1.5)
                            break
                    except:
                        pass

                result["step"] = "location_selected"

                # ── 6. Sélectionne le créneau horaire ─────────
                print(f"[Booker] Étape 5 : Sélection du créneau {slot_datetime}")

                # Extrait l'heure du créneau ex: "17:00" ou "17h00"
                time_str = slot_datetime[-5:] if len(slot_datetime) >= 5 else slot_datetime
                time_variants = [
                    time_str,
                    time_str.replace(":", "h"),
                    time_str.replace(":","h").replace("0","").lstrip("0"),
                ]

                slot_clicked = False
                for tvar in time_variants:
                    try:
                        # Cherche un bouton slot avec cet horaire
                        slot_btn = page.locator(
                            f'button[data-test-id="slot-button"]:has-text("{tvar}")'
                        ).first
                        if slot_btn.is_visible(timeout=3000):
                            slot_btn.click()
                            slot_clicked = True
                            human_delay(1, 2)
                            print(f"  Créneau {tvar} sélectionné !")
                            break
                    except:
                        pass

                if not slot_clicked:
                    # Prend le premier slot disponible
                    try:
                        first_slot = page.locator('button[data-test-id="slot-button"]').first
                        if first_slot.is_visible(timeout=3000):
                            txt = first_slot.inner_text()
                            first_slot.click()
                            slot_clicked = True
                            print(f"  Premier créneau disponible : {txt}")
                            human_delay(1, 2)
                    except:
                        pass

                result["step"] = "slot_selected"

                # ── 7. PAUSE — attend validation utilisateur ──
                print("\n" + "="*55)
                print("  CONFIRMATION REQUISE")
                print("="*55)
                print("  Le tunnel de réservation est rempli.")
                print("  Vérifiez les informations dans le navigateur")
                print("  puis validez manuellement le rendez-vous.")
                print("  (Appuyez sur ENTREE pour fermer le navigateur)")
                print("="*55)

                input("\n[Booker] >>> Appuyez sur ENTREE après avoir confirmé... ")

                result["status"]  = "success"
                result["message"] = "Tunnel complété — confirmation manuelle effectuée"

            except Exception as e:
                result["status"]  = "error"
                result["message"] = str(e)
                print(f"[Booker] Erreur : {e}")
            finally:
                browser.close()

        return result


# ── Route FastAPI pour le booking ────────────────────────────
# À ajouter dans main.py :
#
# class BookRequest(BaseModel):
#     profile_url:     str
#     slot_datetime:   str
#     is_new_patient:  bool = True
#     motive_keyword:  str  = None
#     is_teleconsult:  bool = False
#
# @app.post("/book")
# def book_appointment(req: BookRequest):
#     from doctolib_booking import DoctolibBooker
#     booker = DoctolibBooker(headless=False)
#     result = booker.book(
#         profile_url    = req.profile_url,
#         slot_datetime  = req.slot_datetime,
#         is_new_patient = req.is_new_patient,
#         motive_keyword = req.motive_keyword,
#         is_teleconsult = req.is_teleconsult,
#     )
#     return result


if __name__ == "__main__":
    # Test
    booker = DoctolibBooker(headless=False)
    result = booker.book(
        profile_url    = "https://www.doctolib.fr/gynecologue/paris/juliette-kinn",
        slot_datetime  = "17:00",
        is_new_patient = True,
        motive_keyword = "première",
        is_teleconsult = False,
    )
    print(result)