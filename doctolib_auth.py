"""
Doctolib Auth — Connexion manuelle + 2FA + capture cookies
Utilise page.wait_for_url() pour détecter la connexion de façon fiable.
"""

import json
import os
import time
from datetime import datetime
from playwright.sync_api import sync_playwright

SESSION_FILE = "doctolib_session.json"


def login(email: str = None, password: str = None, headless: bool = False) -> list:
    print(f"\n[Auth] Ouverture du navigateur pour {email}...")

    is_logged = False
    cookies   = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
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

        page.goto("https://www.doctolib.fr/sessions/new", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        # Accepte les cookies si bandeau présent
        for txt in ["Accepter et fermer", "Tout accepter", "Accepter"]:
            try:
                btn = page.locator(f'button:has-text("{txt}")').first
                if btn.is_visible(timeout=1500):
                    btn.click()
                    page.wait_for_timeout(800)
                    break
            except:
                pass

        # Pré-remplit l'email
        if email:
            for sel in ['input[name="username"]', 'input[type="email"]', 'input[id="username"]']:
                try:
                    f = page.locator(sel).first
                    if f.is_visible(timeout=1500):
                        f.fill(email)
                        break
                except:
                    pass

        print("[Auth] Connecte-toi dans la fenetre (email + mot de passe + code 2FA).")
        print("[Auth] La fenetre se fermera automatiquement une fois sur 'Mes rendez-vous'.")

        # ── Attend que l'URL corresponde à une page connectée ────
        # wait_for_url supporte les glob patterns, timeout en ms
        try:
            page.wait_for_url(
                "**/account/**",
                timeout=180_000,   # 3 minutes
                wait_until="domcontentloaded"
            )
            is_logged = True
            print(f"[Auth] Connexion confirmee ! URL : {page.url}")
            page.wait_for_timeout(1500)  # Laisse les cookies se charger

        except Exception as e:
            print(f"[Auth] Timeout ou erreur : {e}")
            print("[Auth] Cookies sauvegardes quand meme.")

        cookies = context.cookies()
        browser.close()

    session_data = {
        "email":        email or "",
        "logged_at":    datetime.now().isoformat(),
        "cookies":      cookies,
        "is_logged":    is_logged,
    }
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(session_data, f, ensure_ascii=False, indent=2)

    print(f"[Auth] {len(cookies)} cookies sauvegardes -> {SESSION_FILE}")
    return cookies


def get_session_cookies() -> list:
    if not os.path.exists(SESSION_FILE):
        raise Exception("Pas de session — connecte-toi via la sidebar SmartRDV")
    with open(SESSION_FILE, encoding="utf-8") as f:
        data = json.load(f)
    print(f"[Auth] Session : {data.get('email','')} (le {data.get('logged_at','')[:10]})")
    return data.get("cookies", [])


def session_info() -> dict:
    if not os.path.exists(SESSION_FILE):
        return {"connected": False, "message": "Pas de session"}
    with open(SESSION_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return {
        "connected":     True,
        "email":         data.get("email", ""),
        "logged_at":     data.get("logged_at", ""),
        "cookies_count": len(data.get("cookies", [])),
        "is_logged":     data.get("is_logged", False),
    }


def logout():
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
        print("[Auth] Session supprimee")


if __name__ == "__main__":
    import sys, getpass
    if len(sys.argv) > 1 and sys.argv[1] == "logout":
        logout()
    elif len(sys.argv) > 1 and sys.argv[1] == "info":
        print(json.dumps(session_info(), indent=2, ensure_ascii=False))
    else:
        em = input("Email Doctolib : ").strip()
        pw = getpass.getpass("Mot de passe : ")
        cookies = login(email=em, password=pw)
        print(f"\n{len(cookies)} cookies sauvegardes")