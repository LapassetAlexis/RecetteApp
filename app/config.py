"""Configuration de l'application via variables d'environnement."""

import os
import secrets
from dataclasses import dataclass, field


@dataclass
class Settings:
    # Notion
    notion_token: str = field(default_factory=lambda: os.getenv("NOTION_TOKEN", ""))
    notion_database_id: str = field(
        default_factory=lambda: os.getenv(
            "NOTION_DATABASE_ID", "1a15a5863e1380e78ebaf2ec3927d33e"
        )
    )

    # Provider LLM (ollama, gemini ou groq)
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "ollama"))

    # Ollama
    ollama_url: str = field(
        default_factory=lambda: os.getenv("OLLAMA_URL", "http://ollama:11434")
    )
    ollama_model: str = field(
        default_factory=lambda: os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
    )

    # Gemini
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-2.0-flash-001"))

    # Groq
    groq_api_key: str = field(default_factory=lambda: os.getenv("GROQ_API_KEY", ""))
    groq_model: str = field(default_factory=lambda: os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"))

    # Auth HTTP Basic optionnelle. Si AUTH_USER et AUTH_PASSWORD sont tous
    # deux renseignés, toutes les routes (sauf /health et /static) exigent ces
    # identifiants. Sinon, aucune authentification (comportement par défaut).
    auth_user: str = field(default_factory=lambda: os.getenv("AUTH_USER", ""))
    auth_password: str = field(default_factory=lambda: os.getenv("AUTH_PASSWORD", ""))

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_user and self.auth_password)

    # App
    app_title: str = field(default_factory=lambda: os.getenv("APP_TITLE", "Menu Planner"))
    # Si SECRET_KEY absent : on génère une clé aléatoire au démarrage plutôt
    # qu'une valeur statique devinable (les sessions seront invalidées à chaque
    # redémarrage, ce qui est acceptable ici — aucune session persistante).
    secret_key: str = field(
        default_factory=lambda: os.getenv("SECRET_KEY") or secrets.token_hex(32)
    )

    # SQLite
    database_path: str = field(
        default_factory=lambda: os.getenv("DATABASE_PATH", "/data/app_recettes.db")
    )


settings = Settings()


def recipe_types(recette: dict) -> list[str]:
    """Types (Repas) d'une recette, toujours sous forme de liste."""
    v = recette.get("repas")
    if isinstance(v, list):
        return [t for t in v if t]
    return [v] if v else []


def recipe_base(recette: dict) -> list[str]:
    """Base (ingrédient principal) d'une recette, toujours sous forme de liste."""
    v = recette.get("base")
    if isinstance(v, list):
        return [b for b in v if b]
    return [v] if v else []


def recipe_nature(recette: dict) -> str:
    """Nature d'une recette : « Recette » (défaut) ou « Ingrédient » (brique)."""
    return recette.get("nature") or "Recette"


# Valeurs autorisées des champs Notion (source unique, partagée par l'app et le
# client LLM pour la classification). Doivent correspondre EXACTEMENT aux options
# de la base Notion.
#
# Taxonomie « une dimension = une propriété » :
#   Nature (select)        : Recette | Ingrédient          (défaut Recette)
#   Repas (multi_select)   : cours du repas                 (REPAS_OPTIONS)
#   Base (multi_select)    : ingrédient principal           (BASE_OPTIONS)
#   Moment (select)        : Midi | Soir | Les deux
#   Tag (multi_select)     : attributs libres               (TAG_OPTIONS)
NATURE_OPTIONS = ["Recette", "Ingrédient"]

REPAS_OPTIONS = [
    "Plat",
    "Entrée",
    "Dessert",
    "Goûter",
    "Apéro",
    "Petit dej",
    "Boisson",
]

BASE_OPTIONS = ["Viande", "Poisson", "Œuf", "Légume", "Féculent", "Végé"]

TAG_OPTIONS = [
    "Soupe",
    "Salade",
    "Quiche/tarte",
    "Tartines",
    "Diet",
    "Végétarien",
    "Fun",
    "Invités",
    "Sur le pouce",
    "1 personne",
    # Effort/temps (rapide en semaine, mijoté le week-end)
    "Rapide",
    "Mijoté",
    # Légèreté (léger plutôt le soir, copieux plutôt le midi)
    "Léger",
    "Copieux",
    # Météo du plat (croise avec la température)
    "Plat chaud",
    "Plat froid",
    # Saison
    "Printemps",
    "Été",
    "Automne",
    "Hiver",
]

# Tags de saison (sous-ensemble de TAG_OPTIONS). Une recette sans aucun de ces
# tags est considérée « toutes saisons ».
SAISON_TAGS = ["Printemps", "Été", "Automne", "Hiver"]

# Regroupement des tags par catégorie pour l'affichage des formulaires.
# Doit couvrir l'ensemble de TAG_OPTIONS (l'ingrédient principal est désormais
# la propriété Base, et le moment la propriété Moment — plus des tags).
TAG_GROUPS = [
    {"label": "🍽️ Type de plat", "tags": ["Soupe", "Salade", "Quiche/tarte", "Tartines"]},
    {"label": "🥗 Régime", "tags": ["Diet", "Végétarien"]},
    {"label": "👥 Occasion", "tags": ["Fun", "Invités", "Sur le pouce", "1 personne"]},
    {"label": "⏱️ Effort", "tags": ["Rapide", "Mijoté"]},
    {"label": "⚖️ Légèreté", "tags": ["Léger", "Copieux"]},
    {"label": "🌡️ Météo du plat", "tags": ["Plat chaud", "Plat froid"]},
    {"label": "🗓️ Saison", "tags": SAISON_TAGS},
]
