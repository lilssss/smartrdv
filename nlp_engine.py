"""
NLP Engine v2 — Extraction complète depuis le prompt
======================================================
Extrait depuis une phrase :
  - specialty        : spécialité médicale
  - location         : ville
  - preferred_hours  : plage horaire
  - prefer_weekend   : bool
  - is_new_patient   : bool (nouvelle consultation ou suivi)
  - motive           : motif de consultation
  - is_teleconsult   : bool (vidéo ou cabinet)
  - intent           : book | chat
  - confidence       : float

Principe : le texte prime sur les préférences.
Si non précisé dans le texte → None (le frontend utilisera les préférences sidebar).
"""

import re
from dataclasses import dataclass, field
from typing import Optional

# ── Dictionnaires ─────────────────────────────────────────────

SPECIALTIES = {
    "dermatologue":        ["dermato", "dermatologue", "dermatologique", "peau", "acné", "grain de beauté", "dermatologie"],
    "gynecologue":         ["gynéco", "gynecologue", "gynécologue", "gynécologie", "gynécologique", "gynecologique", "gynéco-logue"],
    "cardiologue":         ["cardio", "cardiologue", "cardiologique", "coeur", "cœur", "cardiaque", "cardiologie"],
    "medecin-generaliste": ["généraliste", "generaliste", "médecin", "medecin", "docteur", "générale", "généraliste", "médecin traitant"],
    "ophtalmologue":       ["ophtalmo", "ophtalmologue", "ophtalmologique", "yeux", "vue", "vision", "ophtalmologie"],
    "psychiatre":          ["psychiatre", "psychiatrique", "psy", "mental", "anxiété", "dépression", "psychiatrie"],
    "dentiste":            ["dentiste", "dentisterie", "dent", "dentaire"],
    "kinesitherapeute":    ["kiné", "kinesitherapeute", "kinésithérapeute", "kinésithérapie", "rééducation", "kine"],
    "pediatre":            ["pédiatre", "pediatre", "pédiatrique", "pédiatrie", "enfant", "bébé"],
    "orthopediste":        ["orthopédiste", "orthopediste", "orthopédique", "orthopédie", "os", "articulation"],
}

TIME_PATTERNS = [
    (r"apr[eè]s?\s+le\s+travail|apr[eè]s?\s+le\s+boulot|apr[eè]s?\s+le\s+bureau", [18, 20]),
    (r"avant\s+le\s+travail|avant\s+le\s+boulot|avant\s+le\s+bureau",              [7,  9]),
    (r"le\s+soir|ce\s+soir|en\s+soir[ée]e|\bsoir\b",                               [17, 20]),
    (r"le\s+matin\s+t[oô]t|tr[eè]s\s+t[oô]t|matin\s+t[oô]t",                     [7,  9]),
    (r"le\s+matin|dans\s+la\s+matin[ée]e|en\s+matin[ée]e|\bmatin\b",              [8,  12]),
    (r"apr[eè]s[\s\-]?midi|dans\s+l.?\s*apr[eè]s[\s\-]?midi",                     [13, 18]),
    (r"en\s+d[ée]but\s+d.?apr[eè]s[\s\-]?midi",                                   [13, 15]),
    (r"en\s+fin\s+d.?apr[eè]s[\s\-]?midi|fin\s+d.?apr[eè]s[\s\-]?midi",          [16, 18]),
    (r"en\s+fin\s+de\s+journ[ée]e|fin\s+de\s+journ[ée]e",                         [17, 19]),
    (r"\bmidi\b|heure\s+du\s+d[ée]jeuner",                                         [12, 14]),
    (r"\b(\d{1,2})[h:](\d{2})?\b",                                                 None),
]

DAYS = {
    "lundi":0, "mardi":1, "mercredi":2, "jeudi":3,
    "vendredi":4, "samedi":5, "dimanche":6,
}
WEEKEND_WORDS = ["weekend", "week-end", "samedi", "dimanche"]

BOOK_TRIGGERS = [
    "prendre", "rdv", "rendez-vous", "consultation", "voir", "consulter",
    "besoin", "veux", "voudrais", "souhaite", "cherche", "trouver",
    "disponible", "dispo", "créneau", "réserver", "reserver", "booker",
    "je veux", "j'ai besoin",
]

# ── Patterns nouveau/ancien patient ──────────────────────────
NEW_PATIENT_PATTERNS = [
    r"premi[eè]re\s+fois", r"premi[eè]re\s+consultation", r"nouveau\s+patient",
    r"jamais\s+consult", r"premi[eè]re\s+visite", r"premi[eè]re\s+fois",
    r"pour\s+la\s+premi[eè]re", r"\bnouveau\b", r"\bnouvelle\b",
]
RETURNING_PATIENT_PATTERNS = [
    r"suivi", r"renouvellement", r"d[ée]j[aà]\s+consult", r"ancien\s+patient",
    r"ma\s+m[ée]decin", r"mon\s+m[ée]decin", r"habituel", r"d[ée]j[aà]\s+vu",
    r"retour", r"bilan\s+de\s+suivi",
]

# ── Patterns téléconsultation ─────────────────────────────────
TELECONSULT_PATTERNS = [
    r"t[ée]l[ée]consult", r"vid[ée]o", r"\ben\s+ligne\b", r"visio",
    r"[àa]\s+distance", r"par\s+cam[ée]ra",
]
CABINET_PATTERNS = [
    r"cabinet", r"pr[ée]sentiel", r"en\s+personne", r"physique",
    r"sur\s+place", r"en\s+face",
]

# ── Motifs fréquents ─────────────────────────────────────────
MOTIVE_PATTERNS = [
    (r"premi[eè]re\s+consultation|premi[eè]re\s+visite",      "Première consultation"),
    (r"suivi|bilan\s+de\s+suivi|consultation\s+de\s+suivi",   "Consultation de suivi"),
    (r"bilan",                                                  "Bilan"),
    (r"renouvellement|renouveler",                             "Renouvellement d'ordonnance"),
    (r"urgence|\burgent\b",                                    "Urgence"),
    (r"acn[ée]",                                               "Consultation acné"),
    (r"grain\s+de\s+beaut[ée]|naevus",                        "Contrôle grain de beauté"),
    (r"contraception|pilule",                                  "Contraception"),
    (r"grossesse|enceinte",                                    "Suivi grossesse"),
    (r"vaccin",                                                 "Vaccination"),
    (r"douleur",                                               "Consultation douleur"),
]

CITIES = ["paris", "lyon", "marseille", "bordeaux", "toulouse",
          "nantes", "lille", "strasbourg", "nice", "rennes", "montpellier"]


# ── Dataclass résultat ────────────────────────────────────────

@dataclass
class NLPResult:
    intent:             str           = "chat"
    specialty:          str           = "medecin-generaliste"
    location:           str           = "Paris"
    preferred_hours:    Optional[list] = None   # None = utilise les préférences sidebar
    prefer_weekend:     bool          = False
    day:                str           = ""
    preferred_day_num:  Optional[int]  = None   # 0=lundi … 6=dimanche, None = non détecté
    is_new_patient:     Optional[bool] = None   # None = non détecté
    motive:             Optional[str]  = None   # None = non détecté
    is_teleconsult:     Optional[bool] = None   # None = non détecté
    confidence:         float         = 0.0
    message:            str           = ""
    detected:           dict          = field(default_factory=dict)  # ce qui a été détecté


# ── Moteur NLP ────────────────────────────────────────────────

class NLPEngine:

    def analyze(self, text: str) -> NLPResult:
        t = text.lower().strip()
        r = NLPResult()
        det = {}

        # ── Intent ───────────────────────────────────────────
        r.intent = "book" if any(w in t for w in BOOK_TRIGGERS) else "chat"

        # ── Spécialité ────────────────────────────────────────
        best_spec, best_score = None, 0
        for spec, keywords in SPECIALTIES.items():
            for kw in keywords:
                if kw in t and len(kw) > best_score:
                    best_score, best_spec = len(kw), spec
        if best_spec:
            r.specialty = best_spec
            det["specialty"] = best_spec
            r.confidence += 0.3

        # ── Horaires — None si non détecté ───────────────────
        for pattern, hours in TIME_PATTERNS:
            m = re.search(pattern, t)
            if m:
                if hours is None:
                    h = int(m.group(1))
                    mn = int(m.group(2)) if m.lastindex >= 2 and m.group(2) else 0
                    r.preferred_hours = [h, min(h + 2, 20)]
                else:
                    r.preferred_hours = hours
                det["hours"] = r.preferred_hours
                r.confidence += 0.2
                break
        # Si pas détecté → preferred_hours reste None
        # Le frontend utilisera les préférences de la sidebar

        # ── Jour ─────────────────────────────────────────────
        for day_name, day_num in DAYS.items():
            if day_name in t:
                r.day = day_name
                r.preferred_day_num = day_num          # ← FIX : setter le numéro du jour
                det["day"] = day_name
                det["day_num"] = day_num
                if day_num >= 5:
                    r.prefer_weekend = True
                r.confidence += 0.1
                break
        if any(w in t for w in WEEKEND_WORDS):
            r.prefer_weekend = True
            det["weekend"] = True

        # ── Ville ─────────────────────────────────────────────
        for city in CITIES:
            if city in t:
                r.location = city.capitalize()
                det["location"] = r.location
                break

        # ── Nouveau / ancien patient ──────────────────────────
        if any(re.search(p, t) for p in NEW_PATIENT_PATTERNS):
            r.is_new_patient = True
            det["patient_type"] = "nouveau"
            r.confidence += 0.15
        elif any(re.search(p, t) for p in RETURNING_PATIENT_PATTERNS):
            r.is_new_patient = False
            det["patient_type"] = "suivi"
            r.confidence += 0.15

        # ── Téléconsultation / cabinet ────────────────────────
        if any(re.search(p, t) for p in TELECONSULT_PATTERNS):
            r.is_teleconsult = True
            det["mode"] = "téléconsultation"
            r.confidence += 0.1
        elif any(re.search(p, t) for p in CABINET_PATTERNS):
            r.is_teleconsult = False
            det["mode"] = "cabinet"
            r.confidence += 0.1

        # ── Motif de consultation ─────────────────────────────
        for pattern, motive_label in MOTIVE_PATTERNS:
            if re.search(pattern, t):
                r.motive = motive_label
                det["motive"] = motive_label
                r.confidence += 0.1
                break

        r.detected = det
        r.message  = self._build_message(r)
        return r

    def _build_message(self, r: NLPResult) -> str:
        SPEC_LABELS = {
            "dermatologue":"dermatologue", "gynecologue":"gynécologue",
            "cardiologue":"cardiologue", "medecin-generaliste":"médecin généraliste",
            "ophtalmologue":"ophtalmologue", "psychiatre":"psychiatre",
            "dentiste":"dentiste", "kinesitherapeute":"kinésithérapeute",
            "pediatre":"pédiatre", "orthopediste":"orthopédiste",
        }
        if r.intent == "chat":
            return "Je suis votre assistant médical. Dites-moi quelle spécialité vous cherchez !"

        spec   = SPEC_LABELS.get(r.specialty, r.specialty)
        parts  = [f"chez un {spec} à {r.location}"]
        if r.day:
            parts.append(r.day)
        if r.preferred_hours:
            parts.append(f"entre {r.preferred_hours[0]}h et {r.preferred_hours[1]}h")
        else:
            parts.append("selon vos horaires préférés")

        extras = []
        if r.is_new_patient is True:   extras.append("première consultation")
        if r.is_new_patient is False:  extras.append("patient suivi")
        if r.is_teleconsult is True:   extras.append("en téléconsultation")
        if r.is_teleconsult is False:  extras.append("en cabinet")
        if r.motive:                   extras.append(r.motive.lower())

        msg = f"Recherche {' '.join(parts)}"
        if extras:
            msg += f" ({', '.join(extras)})"
        msg += ". Voici le meilleur créneau selon vos critères :"
        return msg


# ── Tests ─────────────────────────────────────────────────────

if __name__ == "__main__":
    engine = NLPEngine()
    tests = [
        "je veux un gynécologue vendredi soir",
        "gynécologue vendredi soir pour une première consultation en cabinet",
        "dermatologue le matin pour un bilan acné, patient suivi",
        "cardiologue disponible en visio",
        "médecin généraliste pour renouvellement d'ordonnance",
        "bonjour comment ça marche",
        "kiné lundi matin pour une rééducation",
    ]
    for t in tests:
        r = engine.analyze(t)
        print(f"\n'{t}'")
        print(f"  intent={r.intent} spec={r.specialty}")
        print(f"  hours={r.preferred_hours} (None=sidebar) new_patient={r.is_new_patient}")
        print(f"  teleconsult={r.is_teleconsult} motive={r.motive}")
        print(f"  detected={r.detected}")