"""M09 — erreurs HTTP cohérentes (D4) : statuts 4xx / 5xx avec un corps
`{"error": …}`, jamais 200 + error, jamais le texte d'une exception, et 400
(non 500) sur un corps illisible."""

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest

import app.main as main
from app.http import MAX_BODY, depuis_service, statut_metier
from tests.test_courses_partage import client, make_planning, share  # noqa: F401
from tests.test_routes import _case, _recipe

APP = Path(__file__).resolve().parent.parent / "app"

# Routes d'action qui lisent un corps JSON (planning 1 / recette « abc »).
ROUTES_JSON = [
    "/api/planning/{pid}/meals",
    "/api/free-meal/{pid}",
    "/api/brique/{pid}",
    "/api/recette/abc/planifier",
    "/api/rate/abc",
    "/api/analyze-url",
    "/api/analyze-text",
    "/api/shopping-check/{pid}",
    "/planning/{pid}/partager",
    "/api/courses/{pid}/extra",
    "/api/courses/{pid}/masquer",
    "/api/courses/{pid}/placard",
    "/api/courses/{pid}/extra/cle/rayon",
    "/courses/{token}/check",
]

SECRET = "détail interne 42"


def _boom(*a, **k):
    raise RuntimeError(SECRET)


async def _aboom(*a, **k):
    raise RuntimeError(SECRET)


async def _notion_ko(*a, **k):
    raise httpx.ConnectError(SECRET)


@pytest.fixture
def planning(client, monkeypatch):  # noqa: F811
    pid = make_planning(client, monkeypatch, {"Gratin": [{"nom": "lait", "quantite": "1", "unite": "l"}]})
    token = share(client, pid).rsplit("/", 1)[1]
    return pid, token


# ── Corps invalide : 400, jamais 500 ────────────────────────────────────


@pytest.mark.parametrize("route", ROUTES_JSON)
@pytest.mark.parametrize("corps", [b"pas du json", b"[1, 2]", b"\xff\xfe", b'"texte"'])
def test_corps_non_json_400(client, planning, route, corps):  # noqa: F811
    pid, token = planning
    url = route.format(pid=pid, token=token)
    r = client.post(url, content=corps, headers={"Content-Type": "application/json"})
    assert r.status_code == 400, (url, r.status_code, r.text)
    assert r.json()["error"]


@pytest.mark.parametrize("route", ROUTES_JSON)
def test_corps_trop_gros_413(client, planning, route):  # noqa: F811
    pid, token = planning
    url = route.format(pid=pid, token=token)
    gros = json.dumps({"texte": "x" * (MAX_BODY + 10)}).encode()
    r = client.post(url, content=gros, headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["error"]


def test_corps_trop_gros_sans_content_length(client, planning):  # noqa: F811
    pid, _ = planning

    def morceaux():
        for _ in range(MAX_BODY // 1024 + 2):
            yield b" " * 1024
    r = client.post(f"/api/courses/{pid}/extra", content=morceaux())
    assert r.status_code == 413


def test_route_publique_corps_invalide_avec_code(client, planning):  # noqa: F811
    _, token = planning
    r = client.post(f"/courses/{token}/check", content=b"{")
    assert r.status_code == 400 and r.json()["code"] == "corps"


def test_corps_vide_accepte_partager(client, planning):  # noqa: F811
    pid, _ = planning
    r = client.post(f"/planning/{pid}/partager")
    assert r.status_code == 200 and r.json()["url"].startswith("/courses/")


# ── 404 : planning introuvable ──────────────────────────────────────────


@pytest.mark.parametrize("methode,url", [
    ("post", "/planning/999/dupliquer"),
    ("post", "/planning/999/valider"),
    ("post", "/planning/999/supprimer"),
    ("get", "/api/planning/999/apercu"),
    ("get", "/api/alternatives/999"),
    ("post", "/generate-shopping/999"),
    ("post", "/planning/999/partager"),
    ("post", "/api/enrich-week/999"),
    ("get", "/api/courses/999/etat"),
])
def test_planning_introuvable_404(client, methode, url):  # noqa: F811
    r = getattr(client, methode)(url)
    assert r.status_code == 404 and r.json()["error"] == "Planning introuvable"


@pytest.mark.parametrize("url,corps", [
    ("/api/shopping-check/999", {"item": "lait", "checked": True}),
    ("/api/planning/999/meals", {"slots": [{"jour": 1, "moment": "midi"}], "action": "set_main", "nom": "X"}),
    ("/api/free-meal/999", {"jour": 1, "moment": "midi", "nom": "X"}),
    ("/api/brique/999", {"jour": 1, "moment": "midi", "slot": "plat", "nom": "X"}),
])
def test_planning_introuvable_json_404(client, monkeypatch, url, corps):  # noqa: F811
    async def _all():
        return []
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    r = client.post(url, json=corps)
    assert r.status_code == 404 and r.json()["error"]


def test_parametres_invalides_400(client, planning):  # noqa: F811
    pid, _ = planning
    assert client.post(f"/api/shopping-check/{pid}", json={}).status_code == 400
    assert client.post("/api/rate/abc", json={"note": "bof"}).status_code == 400
    assert client.post("/api/analyze-url", json={}).status_code == 400
    assert client.post("/api/analyze-text", json={"text": ["liste"]}).status_code == 400
    assert client.post(f"/api/free-meal/{pid}", json={"jour": 9}).status_code == 400
    assert client.post(f"/api/brique/{pid}", json={"jour": 1, "moment": "midi"}).status_code == 400
    assert client.post("/api/recette/abc/planifier", json={"moment": "nuit"}).status_code == 400


def test_generer_sans_plat_400(client, planning):  # noqa: F811
    pid, _ = planning
    data = json.loads(asyncio.run(main.db.get_planning_with_recipes(pid))["data_json"])
    data["plats"] = []
    asyncio.run(main.db.update_planning_data(pid, json.dumps(data)))
    r = client.post(f"/generate-shopping/{pid}")
    assert r.status_code == 400 and r.json()["error"] == "Aucun plat dans ce planning"


# ── 502 / 500 : message générique, exception dans les logs seulement ───


def test_notion_en_echec_502_sans_detail(client, planning, monkeypatch):  # noqa: F811
    pid, _ = planning
    monkeypatch.setattr(main.notion, "update_rating", _aboom)
    monkeypatch.setattr(main.notion, "get_all_recipes", _aboom)
    monkeypatch.setattr(main.notion, "archive_recipe", _aboom)
    monkeypatch.setattr(main.llm, "extract_recipe_from_url", _aboom)
    monkeypatch.setattr(main.llm, "extract_recipe_from_text", _aboom)
    for r in (
        client.post("/api/rate/abc", json={"note": "⭐"}),
        client.get(f"/api/alternatives/{pid}"),
        client.post("/recette/abc/supprimer"),
        client.post("/api/analyze-url", json={"url": "https://exemple.fr"}),
        client.post("/api/analyze-text", json={"text": "Gâteau"}),
    ):
        assert r.status_code == 502, r.request.url
        assert r.json()["error"] and SECRET not in r.text


def test_exception_interne_500_sans_detail(client, planning, monkeypatch):  # noqa: F811
    pid, _ = planning
    from app.routers import courses, recettes
    from app.routers import planning as rplanning

    monkeypatch.setattr(courses, "apply_check", _boom)
    monkeypatch.setattr(courses.store, "update_planning", _aboom)
    monkeypatch.setattr(rplanning, "resolve_meal_action", _aboom)
    monkeypatch.setattr(recettes, "enrich_many", _aboom)
    for r in (
        client.post(f"/api/shopping-check/{pid}", json={"item": "lait", "checked": True}),
        client.post(f"/generate-shopping/{pid}"),
        client.post(f"/planning/{pid}/partager", json={"action": "regenerer"}),
        client.post(f"/api/planning/{pid}/meals",
                    json={"slots": [{"jour": 1, "moment": "midi"}], "action": "set_main", "nom": "X"}),
        client.post(f"/api/enrich-week/{pid}"),
    ):
        assert r.status_code == 500, r.request.url
        assert r.json()["error"] and SECRET not in r.text


def test_creation_externe_en_echec_502(client, planning, monkeypatch):  # noqa: F811
    pid, _ = planning
    monkeypatch.setattr(main.notion, "create_recipe", _notion_ko)
    r = client.post(f"/api/free-meal/{pid}", json={"jour": 1, "moment": "midi", "nom": "Steak"})
    assert r.status_code == 502 and SECRET not in r.text
    r = client.post(f"/api/brique/{pid}", json={"jour": 1, "moment": "midi", "slot": "plat", "nom": "Riz"})
    assert r.status_code == 502 and SECRET not in r.text


def test_construire_erreur_sans_detail(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main.notion, "get_all_recipes", _aboom)
    cases = [_case(1, "midi", _recipe("X", id="x"), persons=2)]
    r = client.post("/construire", data={"week_start": "2026-02-09", "meals": json.dumps(cases)})
    assert r.status_code == 500 and SECRET not in r.text
    monkeypatch.setattr(main.notion, "get_all_recipes", _notion_ko)
    r = client.post("/construire", data={"week_start": "2026-02-09", "meals": json.dumps(cases)})
    assert r.status_code == 502 and SECRET not in r.text


def test_aucun_str_e_dans_les_routeurs():
    for f in (APP / "routers").glob("*.py"):
        src = f.read_text(encoding="utf-8")
        assert not re.search(r"str\((e|exc)\)", src), f.name
        assert 'return {"error"' not in src, f.name


# ── Erreurs du cadre : même forme ───────────────────────────────────────


def test_route_inconnue_et_methode_refusee(client):  # noqa: F811
    r = client.get("/api/nexiste-pas")
    assert r.status_code == 404 and r.json()["error"]
    r = client.get("/planning/1/valider")
    assert r.status_code == 405 and r.json()["error"]


def test_parametre_de_route_invalide_400(client):  # noqa: F811
    r = client.post("/planning/abc/valider")
    assert r.status_code == 400 and r.json()["error"] == "Paramètres invalides"
    r = client.post("/construire", data={})
    assert r.status_code == 400 and r.json()["error"]


# ── Utilitaires ─────────────────────────────────────────────────────────


def test_statut_metier():
    assert statut_metier("Planning introuvable") == 404
    assert statut_metier("Repas non trouvé dans le planning") == 404
    assert statut_metier("Création de la brique impossible") == 502
    assert statut_metier("Paramètres invalides") == 400
    assert depuis_service({"success": True}) == {"success": True}
    assert depuis_service({"error": "x"}, 409).status_code == 409
