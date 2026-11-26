"""L01 — intégrité des brouillons, du cache des ingrédients et du login."""

import asyncio
import json
import re
from datetime import date, timedelta

import pytest
from starlette.testclient import TestClient

from app import main
from app.database import Database
from app.routers import auth as auth_router
from app.services import planning as planning_svc
from tests.test_routes import _case, _construire, _recipe


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbpath = str(tmp_path / "t.db")
    main.db.path = dbpath
    monkeypatch.setattr(main.settings, "database_path", dbpath)

    async def _noop(*a, **k):
        return True
    monkeypatch.setattr(main.notion, "ensure_ingredients_field", _noop)
    with TestClient(main.app) as c:
        yield c


def _catalogue(monkeypatch, *recettes):
    async def _all():
        return list(recettes)
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)


def _initial_builder(html: str) -> list:
    # Grille du constructeur : bloc JSON #page-data (L08).
    m = re.search(r'<script type="application/json" id="page-data">(.*?)</script>', html, re.DOTALL)
    assert m, "page-data absent de la page"
    return json.loads(m.group(1))["builder"]


def _draft_id(html: str) -> str:
    m = re.search(r'id="draft-id-input" value="([^"]*)"', html)
    assert m
    return m.group(1)


def _planning(pid: int) -> dict:
    return asyncio.run(main.db.get_planning_with_recipes(pid))


def _data(pid: int) -> dict:
    return json.loads(_planning(pid)["data_json"])


# ── B1 : la date d'un brouillon re-soumis est mise à jour ─────────────


def test_resoumettre_brouillon_change_week_start(client, monkeypatch):
    poulet = _recipe("Poulet", id="p1")
    _catalogue(monkeypatch, poulet)
    cases = [_case(1, "midi", poulet, persons=2)]
    loc = client.post("/construire", data={
        "week_start": "2026-11-25", "meals": json.dumps(cases), "action": "brouillon",
    }, follow_redirects=False).headers["location"]
    did = int(re.search(r"draft=(\d+)", loc).group(1))

    cases = [_case(1, "midi", poulet, persons=3)]
    r = client.post("/construire", data={
        "week_start": "2026-12-02", "meals": json.dumps(cases), "draft_id": str(did),
    }, follow_redirects=False)
    assert r.headers["location"] == f"/planning/{did}"
    p = _planning(did)
    assert p["week_start"] == "2026-12-02"
    assert p["nb_personnes"] == 3
    # Le titre de la page suit la nouvelle date (mercredi 02/12).
    assert "02/12" in client.get(f"/planning/{did}").text


def test_update_planning_sans_date_garde_week_start(tmp_path):
    db = Database()
    db.path = str(tmp_path / "u.db")
    asyncio.run(db.init())
    pid = asyncio.run(db.save_planning("2026-01-05", "", 4, "", "{}", []))
    asyncio.run(db.update_planning(pid, "{}", []))
    p = asyncio.run(db.get_planning_with_recipes(pid))
    assert p["week_start"] == "2026-01-05" and p["nb_personnes"] == 4
    asyncio.run(db.update_planning(pid, "{}", [], week_start="2026-02-01", nb_personnes=2))
    p = asyncio.run(db.get_planning_with_recipes(pid))
    assert p["week_start"] == "2026-02-01" and p["nb_personnes"] == 2


# ── B2 / B3 : « Refaire » produit un brouillon reprenable, daté d'aujourd'hui ──


def test_refaire_brouillon_reprenable_et_date_du_jour(client, monkeypatch):
    poulet, soupe = _recipe("Poulet", id="p1"), _recipe("Soupe", id="s1")
    _catalogue(monkeypatch, poulet, soupe)
    pid = _construire(client, [_case(1, "midi", poulet, persons=2),
                               _case(1, "soir", soupe, persons=2)],
                      week_start="2026-11-25")
    client.post(f"/planning/{pid}/valider")

    new_id = client.post(f"/planning/{pid}/dupliquer").json()["planning_id"]
    copie = _planning(new_id)
    assert copie["week_start"] == date.today().isoformat()
    data = json.loads(copie["data_json"])
    assert data["builder"] and "off_meals" in data

    html = client.get(f"/?draft={new_id}").text
    builder = _initial_builder(html)
    assert builder
    assert _draft_id(html) == str(new_id)
    midi1 = next(c for c in builder if c["jour"] == 1 and c["moment"] == "midi")
    assert midi1["main"]["nom"] == "Poulet"
    # Le jour 1 de la copie = aujourd'hui (titre de la page planning).
    today = date.today()
    assert f"{today.day:02d}/{today.month:02d}" in client.get(f"/planning/{new_id}").text

    # Reprendre puis enregistrer met à jour la copie, sans 3ᵉ planning.
    client.post("/construire", data={
        "week_start": copie["week_start"], "meals": json.dumps(builder),
        "draft_id": str(new_id),
    }, follow_redirects=False)
    assert len(asyncio.run(main.db.list_drafts())) == 1


def test_refaire_ancien_planning_sans_builder(client, monkeypatch):
    # Ancien planning : pas de `builder` en base → reconstruit depuis les plats.
    plats = [
        {"jour": 2, "moment": "midi", "nom_recette": "Poulet", "notion_id": "p1",
         "persons": 2, "accompagnements": []},
        {"jour": 2, "moment": "soir", "nom_recette": "Soupe", "notion_id": "s1"},
    ]
    pid = asyncio.run(main.db.save_planning(
        "2026-01-05", "", 3, "", json.dumps({"plats": plats, "liste_courses": []}), [],
    ))
    new_id = client.post(f"/planning/{pid}/dupliquer").json()["planning_id"]
    builder = _data(new_id)["builder"]
    soir2 = next(c for c in builder if c["jour"] == 2 and c["moment"] == "soir")
    assert soir2["main"]["nom"] == "Soupe" and soir2["persons"] == 3  # repli nb_personnes
    html = client.get(f"/?draft={new_id}").text
    assert _initial_builder(html) and _draft_id(html) == str(new_id)


def test_reprendre_ancien_brouillon_sans_builder(client):
    plats = [{"jour": 1, "moment": "soir", "nom_recette": "Soupe", "notion_id": "s1",
              "persons": 2}]
    did = asyncio.run(main.db.save_planning(
        "2026-01-05", "", 2, "", json.dumps({"plats": plats}), [],
    ))
    html = client.get(f"/?draft={did}").text
    assert _draft_id(html) == str(did)
    assert any(c.get("main", {}).get("nom") == "Soupe" for c in _initial_builder(html))


# ── B15 / B23 : modification depuis la page planning ──────────────────


def test_update_meal_met_a_jour_la_grille_du_brouillon(client, monkeypatch):
    poulet, soupe, curry = (_recipe("Poulet", id="p1"), _recipe("Soupe", id="s1"),
                            _recipe("Curry", id="c1"))
    _catalogue(monkeypatch, poulet, soupe, curry)
    pid = _construire(client, [_case(1, "midi", poulet, persons=2),
                               _case(1, "soir", soupe, persons=2)])
    r = client.post(f"/api/planning/{pid}/meals",
                    json={"slots": [{"jour": 1, "moment": "midi"}], "action": "set_main", "nom": "Curry"})
    assert r.json().get("success")

    builder = _initial_builder(client.get(f"/?draft={pid}").text)
    midi1 = next(c for c in builder if c["jour"] == 1 and c["moment"] == "midi")
    assert midi1["main"]["nom"] == "Curry" and midi1["main"]["notion_id"] == "c1"
    soir1 = next(c for c in builder if c["jour"] == 1 and c["moment"] == "soir")
    assert soir1["main"]["nom"] == "Soupe"


def test_update_meal_purge_les_coches_orphelines(client, monkeypatch):
    poulet, curry = _recipe("Poulet", id="p1"), _recipe("Curry", id="c1")
    _catalogue(monkeypatch, poulet, curry)
    asyncio.run(main.db.init())
    asyncio.run(main.db.save_enriched("p1", "Poulet", ingredients=json.dumps(
        [{"nom": "poulet", "quantite": "200", "unite": "g"},
         {"nom": "sel", "quantite": "1", "unite": "pincée"}])))
    asyncio.run(main.db.save_enriched("c1", "Curry", ingredients=json.dumps(
        [{"nom": "curry", "quantite": "1", "unite": "c. à café"},
         {"nom": "sel", "quantite": "1", "unite": "pincée"}])))
    pid = _construire(client, [_case(1, "midi", poulet, persons=4)])
    for item in ("poulet", "sel"):
        client.post(f"/api/shopping-check/{pid}", json={"item": item, "checked": True})

    client.post(f"/api/planning/{pid}/meals",
                json={"slots": [{"jour": 1, "moment": "midi"}], "action": "set_main", "nom": "Curry"})
    # « poulet » n'est plus dans la liste → coche retirée ; « sel » reste.
    assert _data(pid)["courses_checked"] == ["sel"]


# ── _builder_from_plats ───────────────────────────────────────────────


def _plat(jour, moment, nom, nid="", persons=2, accs=None, libre=False):
    return {"jour": jour, "moment": moment, "nom_recette": nom, "notion_id": nid,
            "persons": persons, "libre": libre,
            "accompagnements": [{"nom_recette": a, "notion_id": a.lower()}
                                for a in (accs or [])]}


def _slot(builder, jour, moment):
    return next(c for c in builder if c["jour"] == jour and c["moment"] == moment)


def test_builder_from_plats_midis_consecutifs_groupes():
    plats = [_plat(j, "midi", "Lasagnes", "l1") for j in (1, 2, 3)]
    plats.append(_plat(4, "midi", "Quiche", "q1"))
    plats.append(_plat(5, "midi", "Lasagnes", "l1"))  # identique mais non consécutif
    b = planning_svc._builder_from_plats(plats)
    assert len(b) == 14
    assert [_slot(b, j, "midi")["group"] for j in range(1, 6)] == [1, 1, 1, 2, 3]
    # Seul le premier midi du groupe porte le repas (comme le constructeur).
    assert _slot(b, 1, "midi")["main"]["nom"] == "Lasagnes"
    assert "main" not in _slot(b, 2, "midi")
    assert _slot(b, 2, "midi")["persons"] == 2
    # Les soirs n'ont pas de groupe.
    assert "group" not in _slot(b, 1, "soir")


def test_builder_from_plats_accompagnements_distincts_non_groupes():
    plats = [_plat(1, "midi", "Steak", "s1", accs=["Frites"]),
             _plat(2, "midi", "Steak", "s1", accs=["Riz"])]
    b = planning_svc._builder_from_plats(plats)
    assert _slot(b, 2, "midi")["group"] == 2
    assert _slot(b, 1, "midi")["accompagnements"] == [
        {"notion_id": "frites", "nom": "Frites", "nature": "Recette", "libre": False}]


def test_builder_from_plats_repas_libre_et_absences():
    plats = [_plat(1, "soir", "Pizzas", libre=True, persons=3)]
    b = planning_svc._builder_from_plats(plats, off_meals=["1:midi"])
    soir = _slot(b, 1, "soir")
    assert soir["main"] == {"notion_id": "", "nom": "Pizzas", "nature": "Recette", "libre": True}
    assert soir["persons"] == 3
    assert _slot(b, 1, "midi")["persons"] == 0           # absent (off_meals)
    assert _slot(b, 2, "midi")["persons"] == 0           # jour sans repas
    assert _slot(b, 2, "midi")["group"] == 2


def test_builder_from_plats_case_vide_du_jour_garde_les_convives():
    b = planning_svc._builder_from_plats([_plat(1, "midi", "Poulet", "p1", persons=4)])
    soir = _slot(b, 1, "soir")
    assert soir["persons"] == 4 and "main" not in soir


def test_builder_from_plats_ancien_accompagnement_et_persons_absent():
    plats = [{"jour": 1, "moment": "soir", "nom_recette": "Poisson", "notion_id": "f1",
              "accompagnement": {"nom_recette": "Riz", "notion_id": "r1"}}]
    b = planning_svc._builder_from_plats(plats, default_persons=3)
    soir = _slot(b, 1, "soir")
    assert soir["persons"] == 3
    assert [a["nom"] for a in soir["accompagnements"]] == ["Riz"]


# ── B16 : le cache des ingrédients n'écrase plus base/durée/nutrition ──


def test_save_enriched_upsert_partiel(tmp_path):
    db = Database()
    db.path = str(tmp_path / "e.db")
    asyncio.run(db.init())
    asyncio.run(db.save_enriched("n1", "A", ingredients="[1]"))
    got = asyncio.run(db.get_enriched("n1"))
    assert got["base_servings"] == 4 and got["cuisson_minutes"] == 0
    asyncio.run(db.save_enriched("n1", "B", base_servings=6))
    got = asyncio.run(db.get_enriched("n1"))
    assert got["recipe_name"] == "B" and got["ingredients"] == "[1]"
    assert got["base_servings"] == 6


# ── B12 / B13 : login ─────────────────────────────────────────────────


@pytest.mark.parametrize("cible", [
    "https://evil.example", "//evil.example", "/\\evil.example", "\\\\evil.example",
    "javascript:alert(1)", "evil.example", "",
])
def test_login_page_rejette_les_redirections_externes(client, cible):
    r = client.get("/login", params={"next": cible}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_login_page_garde_un_chemin_local(client):
    r = client.get("/login", params={"next": "/historique?x=1"}, follow_redirects=False)
    assert r.headers["location"] == "/historique?x=1"


@pytest.fixture
def auth(monkeypatch):
    monkeypatch.setattr(main.settings, "auth_user", "chef")
    monkeypatch.setattr(main.settings, "auth_password", "sécret")


def test_login_accent_renvoie_401(client, auth):
    r = client.post("/login", data={"username": "é", "password": "é"},
                    follow_redirects=False)
    assert r.status_code == 401


def test_login_mot_de_passe_accentue_ok_et_next_externe_neutralise(client, auth):
    r = client.post("/login", data={"username": "chef", "password": "sécret",
                                    "next": "https://evil.example"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    r = client.post("/login", data={"username": "chef", "password": "sécret",
                                    "next": "/historique"},
                    follow_redirects=False)
    assert r.headers["location"] == "/historique"


def test_safe_next():
    assert auth_router._safe_next("/planning/3") == "/planning/3"
    for bad in ("//x", "/\\x", "http://x", "x", "/a\nb", None):
        assert auth_router._safe_next(bad) == "/"


# ── get_recipe_usage ──────────────────────────────────────────────────


def test_get_recipe_usage(tmp_path):
    db = Database()
    db.path = str(tmp_path / "g.db")
    asyncio.run(db.init())
    def r(nid, nom, jour, moment="midi"):
        return {"notion_id": nid, "recipe_name": nom, "jour": jour, "moment": moment}
    p1 = asyncio.run(db.save_planning("2026-11-25", "", 2, "", "{}", [
        r("p1", "Poulet", 1), r("p1", "Poulet", 3, "soir"), r("", "Pizzas", 2)]))
    p2 = asyncio.run(db.save_planning("2026-11-01", "", 2, "", "{}", [r("s1", "Soupe", 7)]))
    asyncio.run(db.save_planning("2026-12-01", "", 2, "", "{}", [r("p1", "Poulet", 5)]))  # brouillon
    asyncio.run(db.mark_planning_valid(p1))
    asyncio.run(db.mark_planning_valid(p2))

    usage = asyncio.run(db.get_recipe_usage(today=date(2026, 12, 31)))
    assert usage == {
        "p1": {"count": 2, "last": (date(2026, 11, 25) + timedelta(days=2)).isoformat()},
        "s1": {"count": 1, "last": "2026-11-07"},
    }
