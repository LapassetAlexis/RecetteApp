"""Tests de l'auth par session (formulaire de login) optionnelle."""

import importlib
import os

import pytest
from starlette.testclient import TestClient

# Ordre de dépendance : les modules qui font `from app.config import settings`
# (ou `from app.deps import db`) gardent sinon l'ancien objet.
_MODULES = (
    "app.config", "app.database", "app.notion_client", "app.llm_client", "app.deps",
    "app.services.planning", "app.services.store", "app.services.shopping",
    "app.services.nutrition_week",
    "app.services.recettes", "app.services.creations",
    "app.routers.pages", "app.routers.planning", "app.routers.courses",
    "app.routers.recettes", "app.routers.catalogue", "app.routers.auth",
    "app.main",
)


def _reload_app():
    """Recharge la config, les instances partagées, les routeurs puis l'app."""
    mod = None
    for name in _MODULES:
        mod = importlib.reload(importlib.import_module(name))
    return mod


def _client(tmp_path, auth_user="", auth_password=""):
    """Recharge la config + l'app avec l'environnement voulu, sans réseau Notion."""
    os.environ["DATABASE_PATH"] = str(tmp_path / "test.db")
    os.environ["AUTH_USER"] = auth_user
    os.environ["AUTH_PASSWORD"] = auth_password
    os.environ["NOTION_TOKEN"] = ""  # ensure_ingredients_field échouera, c'est capturé

    return TestClient(_reload_app().app)


def test_auth_desactivee_par_defaut(tmp_path):
    with _client(tmp_path) as c:
        assert c.get("/health").status_code == 200
        # route protégée accessible sans login quand l'auth est off
        assert c.get("/historique").status_code == 200


def test_auth_active_redirige_vers_login(tmp_path):
    with _client(tmp_path, "chef", "secret") as c:
        r = c.get("/historique", follow_redirects=False)
        assert r.status_code == 303 and "/login" in r.headers["location"]
        # API → 401 (pas de redirection)
        assert c.get("/api/catalogue", follow_redirects=False).status_code == 401


def test_auth_login_ouvre_session_persistante(tmp_path):
    with _client(tmp_path, "chef", "secret") as c:
        c.post("/login", data={"username": "chef", "password": "secret", "next": "/historique"})
        # session ouverte → route protégée accessible, sans re-login
        assert c.get("/historique").status_code == 200
        # déconnexion → de nouveau protégé
        assert c.get("/logout").status_code == 405   # GET refusé (M09)
        c.post("/logout")
        assert c.get("/historique", follow_redirects=False).status_code == 303


def test_auth_refuse_mauvais_mot_de_passe(tmp_path):
    with _client(tmp_path, "chef", "secret") as c:
        r = c.post("/login", data={"username": "chef", "password": "mauvais"},
                   follow_redirects=False)
        assert r.status_code == 401
        assert c.get("/historique", follow_redirects=False).status_code == 303  # toujours protégé


def test_health_et_login_publics_meme_avec_auth(tmp_path):
    with _client(tmp_path, "chef", "secret") as c:
        assert c.get("/health").status_code == 200
        assert c.get("/login").status_code == 200


@pytest.fixture(autouse=True)
def _cleanup_env():
    yield
    for k in ("DATABASE_PATH", "AUTH_USER", "AUTH_PASSWORD", "NOTION_TOKEN"):
        os.environ.pop(k, None)
    # Restaurer un app.main propre (sans middleware d'auth) pour les autres
    # fichiers de tests : le reload ci-dessus laisse sinon l'auth activée sur le
    # module partagé.
    _reload_app()
