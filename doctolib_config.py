"""
Pour utiliser les vrais headers Doctolib :

1. Va sur https://www.doctolib.fr → cherche "dermatologue Paris"
2. F12 → Network → Fetch/XHR → trouve search_results.json
3. Clic droit → Copy → Copy as fetch
4. Copie les valeurs de "cookie" et "x-csrf-token" ci-dessous
"""

# ============================================================
# COLLE TES VALEURS ICI
# ============================================================

REAL_COOKIE = ""          # colle la valeur du cookie ici
REAL_CSRF_TOKEN = ""      # colle la valeur du x-csrf-token ici

# ============================================================
# Si les deux champs sont vides → le code utilise les mocks
# Si remplis → il appelle la vraie API Doctolib
# ============================================================
