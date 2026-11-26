"""M09 — durcissement : déconnexion en POST, limite des échecs de connexion,
flux d'enrichissement non déclenchable par une navigation, planning illisible
en 422, `?draft=` d'une semaine validée redirigé."""

import asyncio
import json
import os

import pytest

import app.main as main
from app.routers import auth
from tests.aides import _case, _construire, _recipe
from tests.test_auth import _client, _reload_app
from tests.test_courses_partage import make_planning

# ── Connexion / déconnexion ─────────────────────────────────────────────


@pytest.fixture
def client_auth(tmp_path):
    with _client(tmp_path, "chef", "secret") as c:
        yield c
    for k in ("DATABASE_PATH", "AUTH_USER", "AUTH_PASSWORD", "NOTION_TOKEN"):
        os.environ.pop(k, None)
    # L'app a été rechargée avec l'auth : on remet une app sans auth pour la suite.
    _reload_app()


def _login(c, password="secret", **kw):
    return c.post("/login", data={"username": "chef", "password": password, "next": "/historique"},
                  follow_redirects=False, **kw)


def test_logout_get_refuse_post_deconnecte(client_auth):
    c = client_auth
    assert _login(c).status_code == 303
    html = c.get("/historique").text
    assert '<form method="post" action="/logout" class="nav-logout">' in html
    assert 'href="/logout"' not in html
    assert c.get("/logout", follow_redirects=False).status_code == 405
    assert c.get("/historique", follow_redirects=False).status_code == 200   # toujours connecté
    r = c.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/historique", follow_redirects=False).status_code == 303


def test_api_sans_session_401_json(client_auth):
    r = client_auth.get("/api/catalogue")
    assert r.status_code == 401
    assert r.json() == {"error": "Authentification requise", "code": "auth"}


def test_login_cinq_echecs_puis_429(client_auth):
    c = client_auth
    auth._echecs.clear()
    for _ in range(auth.LOGIN_LIMIT):
        assert _login(c, "mauvais").status_code == 401
    r = _login(c, "mauvais")
    assert r.status_code == 429 and "Trop de tentatives" in r.text
    # Bon mot de passe refusé lui aussi tant que la fenêtre court.
    assert _login(c).status_code == 429
    auth._echecs.clear()
    assert _login(c).status_code == 303


def test_limite_login_fenetre_glissante():
    auth._echecs.clear()
    for t in range(auth.LOGIN_LIMIT):
        auth._noter_echec("1.2.3.4", now=100.0 + t)
    assert auth._bloque("1.2.3.4", now=105.0)
    assert not auth._bloque("5.6.7.8", now=105.0)
    # Les échecs sortent de la fenêtre un par un.
    assert not auth._bloque("1.2.3.4", now=100.0 + auth.LOGIN_WINDOW)
    auth._echecs.clear()


def test_limite_login_purge_les_adresses_inactives():
    auth._echecs.clear()
    for i in range(1001):
        auth._noter_echec(f"10.0.{i // 256}.{i % 256}", now=0.0)
    auth._noter_echec("9.9.9.9", now=1000.0)
    assert list(auth._echecs) == ["9.9.9.9"]
    auth._echecs.clear()


# ── Enrichissement de masse ─────────────────────────────────────────────


def test_flux_enrichissement_refuse_une_navigation(client, monkeypatch):
    appels = []

    async def _all():
        appels.append(1)
        return []
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    r = client.post("/api/enrich-all/stream", headers={"Accept": "text/html,application/xhtml+xml"})
    assert r.status_code == 405 and r.json()["error"]
    assert appels == []
    r = client.post("/api/enrich-all/stream", headers={"Accept": "text/event-stream"})
    assert r.status_code == 200 and "event: done" in r.text


def test_flux_enrichissement_erreur_sans_detail(client, monkeypatch):
    async def _ko():
        raise RuntimeError("jeton Notion secret")
    monkeypatch.setattr(main.notion, "get_all_recipes", _ko)
    body = client.post("/api/enrich-all/stream").text
    assert "event: error" in body and "secret" not in body


# ── Page planning illisible ─────────────────────────────────────────────


def _planning_brut(client, monkeypatch, data_json: str) -> int:
    poulet = _recipe("Poulet", id="p1")

    async def _all():
        return [poulet]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    pid = _construire(client, [_case(1, "midi", poulet, persons=2)])
    asyncio.run(main.db.update_planning_data(pid, data_json))
    return pid


@pytest.mark.parametrize("data_json", [
    "pas du json",
    "[]",
    json.dumps({"plats": "liste"}),
    json.dumps({"plats": [{"jour": "lundi", "moment": "midi", "nom_recette": "X"}]}),
    json.dumps({"plats": [{"jour": 9, "moment": "midi", "nom_recette": "X"}]}),
    json.dumps({"plats": [{"jour": 1, "moment": "goûter", "nom_recette": "X"}]}),
    json.dumps({"plats": [{"jour": 1, "moment": "midi"}]}),
    json.dumps({"plats": ["X"]}),
])
def test_planning_illisible_422(client, monkeypatch, data_json):
    pid = _planning_brut(client, monkeypatch, data_json)
    r = client.get(f"/planning/{pid}")
    assert r.status_code == 422
    assert "Planning illisible" in r.text and "mal formés" in r.text
    assert 'class="nav"' in r.text                     # gabarit de base, menu compris


def test_planning_sans_plats_reste_lisible(client, monkeypatch):
    pid = _planning_brut(client, monkeypatch, json.dumps({}))
    assert client.get(f"/planning/{pid}").status_code == 200


# ── ?draft= d'une semaine validée ───────────────────────────────────────


def test_draft_d_une_semaine_validee_redirige(client, monkeypatch):
    pid = make_planning(client, monkeypatch, {"Gratin": []}, generate=False)
    # Brouillon : ouvert tel quel.
    assert client.get(f"/?draft={pid}", follow_redirects=False).status_code == 200
    client.post(f"/planning/{pid}/valider")
    r = client.get(f"/?draft={pid}&saved=1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/?planning={pid}&saved=1"
    html = client.get(f"/?draft={pid}").text            # redirection suivie
    assert f'name="draft_id" id="draft-id-input" value="{pid}"' in html
