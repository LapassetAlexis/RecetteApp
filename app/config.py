"""Configuration de l'application via variables d'environnement."""

import os
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

    # Ollama
    ollama_url: str = field(
        default_factory=lambda: os.getenv("OLLAMA_URL", "http://ollama:11434")
    )
    ollama_model: str = field(
        default_factory=lambda: os.getenv("OLLAMA_MODEL", "llama3.2:1b")
    )

    # App
    app_title: str = field(default_factory=lambda: os.getenv("APP_TITLE", "Menu Planner"))
    secret_key: str = field(
        default_factory=lambda: os.getenv("SECRET_KEY", "change-me-in-production")
    )

    # SQLite
    database_path: str = field(
        default_factory=lambda: os.getenv("DATABASE_PATH", "/data/app_recettes.db")
    )


settings = Settings()
