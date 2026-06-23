"""Textes de l'interface en français et en arabe."""

# Invite d'accueil (écran de repos) — bilingue
SCAN_PROMPT_FR = "Scannez le code-barres pour vérifier le prix"
SCAN_PROMPT_AR = "امسح الباركود لمعرفة السعر"

# Article introuvable
NOT_FOUND_FR = "Article introuvable"
NOT_FOUND_AR = "المنتج غير موجود"

# Erreur de connexion au serveur
DB_ERROR_FR = "Erreur de connexion au serveur"
DB_ERROR_AR = "خطأ في الاتصال بالخادم"

# Libellés divers
PRICE_LABEL_FR = "Prix"
CURRENCY = "DA"  # Dinar algérien

# Assistant de configuration
SETUP_TITLE = "Configuration — Affichage Prix Netfact"
SETUP_SERVER = "Serveur (nom du PC ou IP)"
SETUP_PORT = "Port"
SETUP_DATABASE = "Chemin de la base (.FDB)"
SETUP_USER = "Utilisateur"
SETUP_PASSWORD = "Mot de passe"
SETUP_WOO_URL = "Adresse du site WooCommerce"
SETUP_WOO_KEY = "Clé API (consumer key)"
SETUP_WOO_SECRET = "Secret API (consumer secret)"
SETUP_TEST = "Tester la connexion"
SETUP_SAVE = "Enregistrer et démarrer"
SETUP_TEST_OK = "Connexion réussie"
SETUP_TEST_FAIL = "Échec de la connexion"

# Formatage du prix : 1 250,00 DA
def format_price(value: float) -> str:
    s = f"{value:,.2f}"  # 1,250.00
    s = s.replace(",", " ").replace(".", ",")  # 1 250,00
    return f"{s} {CURRENCY}"
