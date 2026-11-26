"""Instances partagées de l'application (base, Notion, LLM, templates) et
constantes métier communes aux routeurs et aux services.

Une seule instance de chaque client : `app.main` les ré-exporte, et les tests
les simulent en remplaçant leurs méthodes (`main.notion.<méthode> = …`)."""

import time
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.config import TAG_GROUPS
from app.database import Database
from app.llm_client import LLMClient
from app.notion_client import NotionClient
from app.text_utils import clean_recipe_title

VERSION = "1.1.0"

BASE_DIR = Path(__file__).parent

BASE_SERVINGS = 4  # base d'extraction des ingrédients (cf. extract_ingredients)

# État attribué à une recette dès qu'on la note (doit correspondre EXACTEMENT à
# une option « status » de Notion : To-do « À essayer », In progress
# « Prochaine recette », Complete « Réussie »/« Testée »). Noter = testé (pas
# forcément réussi), donc « Testée ».
RATED_STATUS = "Testée"

# Instances
db = Database()
notion = NotionClient()
llm = LLMClient()

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
# Version dans tous les templates
templates.env.globals["version"] = VERSION
# Identifiant d'asset (change à chaque démarrage) → casse le cache CSS/SW au
# redéploiement sans dépendre du SW pour rafraîchir.
templates.env.globals["asset_version"] = str(int(time.time()))
# Filtre de nettoyage des titres de recettes (retire les suffixes de site)
templates.env.filters["clean_title"] = clean_recipe_title
# Tags groupés par catégorie (affichage des formulaires)
templates.env.globals["tag_groups"] = TAG_GROUPS
