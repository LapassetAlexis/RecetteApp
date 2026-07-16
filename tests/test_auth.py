"""Tests de l'auth par session (formulaire de login) optionnelle."""

import importlib
import os

import pytest
from starlette.testclient import TestClient


def _client(tmp_path, auth_user="", auth_password=""):
    """Recharge la config + l'app avec l'environnement voulu, sans réseau Notion."""
    os.environ["DATABASE_PATH"] = str(tmp_path / "test.db")
    os.environ["AUTH_USER"] = auth_user
    os.environ["AUTH_PASSWORD"] = auth_password
    os.environ["NOTION_TOKEN"] = ""  # ensure_ingredients_field échouera, c'est capturé

    # Recharger en ordre de dépendance : les modules qui font
    # `from app.config import settings` gardent sinon l'ancien singleton.
    import app.config, app.database, app.notion_client, app.llm_client, app.main
    importlib.reload(app.config)
    importlib.reload(app.database)
    importlib.reload(app.notion_client)
    importlib.reload(app.llm_client)
    main = importlib.reload(app.main)
    return TestClient(main.app)


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
        c.get("/logout")
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
    import app.config, app.database, app.notion_client, app.llm_client, app.main
    importlib.reload(app.config)
    importlib.reload(app.database)
    importlib.reload(app.notion_client)
    importlib.reload(app.llm_client)
    importlib.reload(app.main)
