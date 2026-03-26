"""
Doctolib Auth — Connexion compte patient
==========================================
Se connecte une seule fois avec email + mot de passe,
sauvegarde les cookies de session dans un fichier local,
les réutilise automatiquement pour tous les bookings suivants.

Usage :
    python doctolib_auth.py          → connexion manuelle
    from doctolib_auth import get_session_cookies
"""

import json
import os
import getpass
from datetime import datetime
from playwright.sync_api import sync_playwright

SESSION_FILE = "doctolib_session.json"

# ============================================================
# CONNEXION
# ============================================================

def login(email: str = None, password: str = None, headless: bool = False) -> dict:
    """
    Ouvre Doctolib, connecte le compte patient, sauvegarde les cookies.
    Retourne les cookies de session.
    """
    if not email:
        email    = input("[Auth] Email Doctolib : ").strip()
    if not password:
        password = getpass.getpass("[Auth] Mot de passe : ")

    print(f"\n[Auth] Connexion en cours pour {email}...")

    cookies = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="fr-FR",
        )
        page = context.new_page()

        # ── Ouvre Doctolib ────────────────────────────────────
        page.goto("https://www.doctolib.fr", wait_until="domcontentloaded")

        # Accepte les cookies
        for txt in ["Accepter", "ACCEPTER"]:
            try:
                btn = page.get_by_text(txt, exact=True)
                if btn.is_visible(timeout=2000):
                    btn.click()
                    page.wait_for_timeout(800)
                    break
            except:
                pass

        # ── Clique "Se connecter" ─────────────────────────────
        try:
            btn = page.get_by_text("Se connecter", exact=False)
            if btn.is_visible(timeout=3000):
                btn.click()
                page.wait_for_timeout(1500)
        except:
            page.goto("https://www.doctolib.fr/sessions/new")
            page.wait_for_timeout(1500)

        # ── Remplit email + mot de passe ──────────────────────
        try:
            # Email
            email_field = page.locator('input[type="email"], input[name="email"], input[id*="email"]').first
            email_field.wait_for(state="visible", timeout=5000)
            email_field.fill(email)
            page.wait_for_timeout(500)

            # Mot de passe
            pwd_field = page.locator('input[type="password"]').first
            pwd_field.wait_for(state="visible", timeout=5000)
            pwd_field.fill(password)
            page.wait_for_timeout(500)

            # Soumet le formulaire
            pwd_field.press("Enter")
            page.wait_for_timeout(3000)

            print("[Auth] Formulaire soumis...")

        except Exception as e:
            print(f"[Auth] Erreur formulaire : {e}")
            print("[Auth] Connecte-toi manuellement dans le navigateur...")
            input("[Auth] >>> Appuie sur ENTREE une fois connecté... ")

        # ── Vérifie la connexion ──────────────────────────────
        is_logged = page.evaluate("""
            () => document.body.innerHTML.includes('Mon compte') ||
                  document.body.innerHTML.includes('Mes rendez-vous') ||
                  !!document.querySelector('[data-test="account-menu"]') ||
                  !!document.querySelector('[href*="mes-rdv"]')
        """)

        if is_logged:
            print("[Auth] ✅ Connecté !")
        else:
            print("[Auth] ⚠️  Connexion non détectée")
            # 2FA ou autre vérification ?
            title = page.title()
            print(f"[Auth] Page actuelle : {title}")
            if "vérification" in title.lower() or "code" in title.lower():
                print("[Auth] Une vérification 2FA est peut-être requise")
                print("[Auth] Complète-la dans le navigateur puis appuie sur ENTREE")
                input("[Auth] >>> ")

        # ── Sauvegarde les cookies ────────────────────────────
        cookies = context.cookies()
        browser.close()

    # Sauvegarde dans le fichier session
    session_data = {
        "email":      email,
        "logged_at":  datetime.now().isoformat(),
        "cookies":    cookies,
    }
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(session_data, f, ensure_ascii=False, indent=2)

    print(f"[Auth] Session sauvegardée ({len(cookies)} cookies) → {SESSION_FILE}")
    return cookies


# ============================================================
# CHARGEMENT DE LA SESSION
# ============================================================

def get_session_cookies() -> list:
    """
    Retourne les cookies de session sauvegardés.
    Si pas de session ou session expirée → relance la connexion.
    """
    if not os.path.exists(SESSION_FILE):
        print("[Auth] Pas de session — connexion requise")
        return login()

    with open(SESSION_FILE, encoding="utf-8") as f:
        data = json.load(f)

    cookies   = data.get("cookies", [])
    logged_at = data.get("logged_at", "")
    email     = data.get("email", "")

    print(f"[Auth] Session existante : {email} (connecté le {logged_at[:10]})")

    # Vérifie si la session est encore valide
    if _is_session_valid(cookies):
        print("[Auth] ✅ Session valide — réutilisation")
        return cookies
    else:
        print("[Auth] Session expirée — reconnexion nécessaire")
        return login(email=email)


def _is_session_valid(cookies: list) -> bool:
    """
    Vérifie rapidement si les cookies Doctolib sont encore valides.
    """
    doctolib_cookies = [c for c in cookies if "doctolib" in c.get("domain", "")]
    if not doctolib_cookies:
        return False

    # Cherche le cookie de session principal
    session_cookie = next(
        (c for c in cookies if "_doctolib_session" in c.get("name", "")),
        None
    )
    if not session_cookie:
        return False

    # Vérifie l'expiration si disponible
    expires = session_cookie.get("expires", -1)
    if expires > 0:
        import time
        if time.time() > expires:
            return False

    return True


def session_info() -> dict:
    """Retourne les infos de la session courante."""
    if not os.path.exists(SESSION_FILE):
        return {"connected": False, "message": "Pas de session"}
    with open(SESSION_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return {
        "connected":  True,
        "email":      data.get("email", ""),
        "logged_at":  data.get("logged_at", ""),
        "cookies_count": len(data.get("cookies", [])),
    }


def logout():
    """Supprime la session sauvegardée."""
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
        print("[Auth] Session supprimée")
    else:
        print("[Auth] Pas de session à supprimer")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "logout":
        logout()
    elif len(sys.argv) > 1 and sys.argv[1] == "info":
        info = session_info()
        print(json.dumps(info, indent=2, ensure_ascii=False))
    else:
        cookies = login()
        print(f"\n✅ Connecté — {len(cookies)} cookies sauvegardés")
        print("Tu peux maintenant utiliser SmartRDV pour réserver automatiquement.")
