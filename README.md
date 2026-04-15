# kAIros — Assistant IA de Planning

> Trouvez le meilleur créneau médical ou bien-être en tenant compte de votre calendrier, vos trajets et vos préférences — via Telegram.

---

## 👥 Auteurs

| Nom | Email |
|-----|-------|
| Julie BOTTI | julie.botti@skema.edu |
| Lila KANOUN | lila.kanoun@skema.edu |
| Rayane Wassim BABA ALI | rayanewassim.babaali@skema.edu |

---

## 🧠 Fonctionnalités

- **Bot Telegram** — Interface conversationnelle en langage naturel
- **NLP Gemini** — Extraction des intentions, dates, horaires et spécialités
- **Crawler Doctolib** — Récupération des créneaux médicaux via Playwright
- **Crawler Planity** — Récupération des créneaux bien-être/beauté via Playwright
- **Scoring intelligent** — 5 critères : conflits agenda, trajets Maps, préférences horaires, fatigue, interruption
- **Google Calendar** — Détection des conflits avant de proposer un créneau
- **Google Maps** — Calcul du temps de trajet depuis votre position
- **Gmail Parser** — Détection automatique d'événements dans vos mails
- **Cache** — Réutilisation des données crawlées (< 2h) pour éviter les attentes

---

## 🏗️ Architecture

```
smartrdv/
├── main.py                 # Backend FastAPI (port 8000)
├── telegram_bot.py         # Bot Telegram
├── planity_crawler.py      # Crawler Playwright — Planity
├── planity_client.py       # Adapter + scoring Planity
├── doctolib_crawler.py     # Crawler Playwright — Doctolib
├── doctolib_client.py      # Adapter + scoring Doctolib
├── calendar_scheduler.py   # Scoring Calendar + Maps
├── google_calendar.py      # Intégration Google Calendar API
├── gmail_parser.py         # Parser Gmail + Gemini
├── scheduler_optimizer.py  # Moteur de scoring et ranking
├── database.py             # SQLite + SQLAlchemy
├── .env                    # Variables d'environnement (non versionné)
└── requirements.txt        # Dépendances Python
```

---

## ⚙️ Prérequis

- Python 3.11+
- Node.js (optionnel)
- Un compte Google Cloud avec les APIs suivantes activées :
  - Generative Language API (Gemini)
  - Google Calendar API
  - Gmail API
  - Maps Distance Matrix API
  - Geocoding API
- Un bot Telegram créé via [@BotFather](https://t.me/BotFather)

---

## 🚀 Installation

### 1. Cloner le dépôt

```bash
git clone https://github.com/lilssss/smartrdv.git
cd smartrdv
```

### 2. Créer un environnement virtuel

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate
```

### 3. Installer les dépendances Python

```bash
pip install -r requirements.txt
```

### 4. Installer Playwright et les navigateurs

```bash
playwright install chromium
```

### 5. Configurer les variables d'environnement

Créer un fichier `.env` à la racine du projet :

```env
# IA
GOOGLE_API_KEY=AIza...          # Gemini + Geocoding
ANTHROPIC_API_KEY=sk-ant-...    # Claude (fallback optionnel)

# Maps
MAPS_API_KEY=AIza...            # Distance Matrix API

# Telegram
TELEGRAM_TOKEN=...              # Token BotFather
```

### 6. Configurer Google OAuth2 (Calendar + Gmail)

1. Aller sur [Google Cloud Console](https://console.cloud.google.com)
2. Créer un projet et activer les APIs : Calendar, Gmail, Generative Language, Maps
3. Créer des identifiants OAuth 2.0 → télécharger `credentials.json`
4. Placer `credentials.json` à la racine du projet
5. Au premier lancement, une fenêtre de connexion Google s'ouvrira pour générer `token.json`

---

## ▶️ Lancement

Ouvrir **deux terminaux** :

**Terminal 1 — Backend FastAPI**
```bash
python -m uvicorn main:app --reload --port 8000
```

**Terminal 2 — Bot Telegram**
```bash
python telegram_bot.py
```

Puis ouvrir Telegram, chercher votre bot et envoyer `/start`.

---

## 💬 Utilisation

### Exemples de commandes Telegram

```
Je veux un coiffeur cet après-midi à Paris
Gynécologue vendredi soir
Dermatologue le 25 avril après le travail
Massage samedi matin
```

### Commandes bot

| Commande | Description |
|----------|-------------|
| `/start` | Démarrer le bot |
| `/status` | État du backend et du scan Gmail |
| `/scan` | Forcer un scan Gmail immédiat |
| `/annuler` | Annuler la recherche en cours |

---

## 🔧 Variables d'environnement

| Variable | Description | Obligatoire |
|----------|-------------|-------------|
| `GOOGLE_API_KEY` | Clé Gemini + Geocoding API | ✅ |
| `MAPS_API_KEY` | Google Maps Distance Matrix | ✅ |
| `TELEGRAM_TOKEN` | Token du bot Telegram | ✅ |
| `ANTHROPIC_API_KEY` | Claude (fallback si Gemini KO) | ❌ |

---

## 🗂️ Fichiers ignorés par Git

Les fichiers suivants sont exclus du dépôt (données locales) :

```
.env
slots.json
planity_slots.json
smartrdv.db
token.json
credentials.json
```

---

## 📦 Dépendances principales

```
fastapi
uvicorn
playwright
python-telegram-bot
google-generativeai
google-auth-oauthlib
google-api-python-client
sqlalchemy
requests
python-dotenv
anthropic
```

---

## 📄 Licence

Projet académique — SKEMA Business School
