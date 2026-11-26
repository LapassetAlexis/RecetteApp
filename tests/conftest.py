"""Fixtures communes de la suite (hors e2e, qui a son propre `tests/e2e/conftest.py`).

- `client` : TestClient de l'application sur une base SQLite temporaire, sans
  réseau Notion (`ensure_ingredients_field` neutralisé au démarrage). Chaque
  test simule ensuite les appels Notion/LLM dont il a besoin (monkeypatch).
- `aujourdhui` : fige `date.today()` dans les modules qui s'en servent, pour
  qu'un test ne dépende pas de l'heure à laquelle il tourne (ex. minuit).
"""

import sys
from datetime import date

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Lu à l'appel : des tests rechargent `app.main` (`tests.test_auth._reload_app`).
    main = sys.modules.get("app.main") or __import__("app.main", fromlist=["app"])
    dbpath = str(tmp_path / "t.db")
    main.db.path = dbpath
    # Les services écrivent via settings.database_path : l'aligner aussi.
    monkeypatch.setattr(main.settings, "database_path", dbpath)

    async def _noop(*a, **k):
        return True
    monkeypatch.setattr(main.notion, "ensure_ingredients_field", _noop)
    with TestClient(main.app) as c:
        yield c


# Modules qui appellent `date.today()` (importés par `from datetime import date`).
MODULES_DATE = (
    "app.database",
    "app.routers.pages",
    "app.routers.planning",
    "app.services.recette_planifier",
    "app.services.recettes",
)

JOUR_FIXE = date(2026, 11, 25)   # un mercredi


class _DateFigee(date):
    """`date` dont `today()` renvoie `JOUR_FIXE` (le reste est inchangé)."""

    @classmethod
    def today(cls):
        return cls(JOUR_FIXE.year, JOUR_FIXE.month, JOUR_FIXE.day)


@pytest.fixture
def aujourdhui(monkeypatch) -> date:
    """Fige `date.today()` au mercredi 25/11/2026 dans l'app ; renvoie ce jour."""
    import importlib
    for nom in MODULES_DATE:
        mod = importlib.import_module(nom)
        if getattr(mod, "date", None) is not None:
            monkeypatch.setattr(mod, "date", _DateFigee)
    return _DateFigee.today()
