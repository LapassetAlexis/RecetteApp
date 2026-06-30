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


# Valeurs autorisées des champs Notion (source unique, partagée par l'app et le
# client LLM pour la classification). Doivent correspondre EXACTEMENT aux options
# de la base Notion.
REPAS_OPTIONS = [
    "Plat",
    "Dessert",
    "Entrée",
    "Goûter",
    "Accompagnement",
    "Apéro",
    "Boisson",
    "Petit dej",
    "Légume",
]
TAG_OPTIONS = [
    "Viande",
    "Poisson",
    "Légumes",
    "Soupe",
    "Salade",
    "Diet",
    "Fun",
    "Quiche/tarte",
    "Tartines",
    "Invités",
    "Sur le pouce",
    "Végétarien proténiné",
    "1 personne",
    "Printemps",
    "Été",
    "Automne",
    "Hiver",
    # Moment de repas (oriente l'attribution midi/soir)
    "Midi",
    "Soir",
    # Légèreté (léger plutôt le soir, copieux plutôt le midi)
    "Léger",
    "Copieux",
    # Effort/temps (rapide en semaine, mijoté le week-end)
    "Rapide",
    "Mijoté",
    # Météo du plat (croise avec la température)
    "Plat chaud",
    "Plat froid",
]

# Tags de saison (sous-ensemble de TAG_OPTIONS). Une recette sans aucun de ces
# tags est considérée « toutes saisons ».
SAISON_TAGS = ["Printemps", "Été", "Automne", "Hiver"]

# Regroupement des tags par catégorie pour l'affichage des formulaires.
# Doit couvrir l'ensemble de TAG_OPTIONS.
TAG_GROUPS = [
    {"label": "🥩 Ingrédient principal", "tags": ["Viande", "Poisson", "Légumes"]},
    {"label": "🍽️ Type de plat", "tags": ["Soupe", "Salade", "Quiche/tarte", "Tartines"]},
    {"label": "🥗 Régime", "tags": ["Diet", "Végétarien proténiné"]},
    {"label": "👥 Occasion", "tags": ["Fun", "Invités", "Sur le pouce", "1 personne"]},
    {"label": "🕑 Moment", "tags": ["Midi", "Soir"]},
    {"label": "⚖️ Légèreté", "tags": ["Léger", "Copieux"]},
    {"label": "⏱️ Effort", "tags": ["Rapide", "Mijoté"]},
    {"label": "🌡️ Météo du plat", "tags": ["Plat chaud", "Plat froid"]},
    {"label": "🗓️ Saison", "tags": SAISON_TAGS},
]
