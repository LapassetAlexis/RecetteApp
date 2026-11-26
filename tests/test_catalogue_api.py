"""L12 — /api/catalogue enrichi (tags, note, état, durée, usage) et statut
d'erreur quand Notion échoue ; gabarit du picker."""

import asyncio
import json

import pytest
from starlette.testclient import TestClient

from app import main
from tests.test_routes import _case, _recipe


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


def _catalogue(monkeypatch, recettes):
    async def _all():
        return recettes
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)


def test_champs_enrichis_sans_cache(client, monkeypatch):
    r = _recipe("Curry", id="c1", tags=["Rapide", "Hiver"], etat="À essayer")
    r["note"] = "⭐⭐⭐⭐"
    _catalogue(monkeypatch, [r])
    res = client.get("/api/catalogue")
    assert res.status_code == 200
    [item] = res.json()
    assert item["id"] == "c1" and item["nom"] == "Curry"
    assert item["tags"] == ["Rapide", "Hiver"]
    assert item["note"] == "⭐⭐⭐⭐"
    assert item["etat"] == "À essayer"
    assert item["duree"] == 0
    assert item["usage"] == {"count": 0, "last": ""}
    # Champs historiques toujours présents (compatibilité du picker).
    for k in ("nature", "base", "repas", "image"):
        assert k in item


def test_champs_manquants_ont_un_repli(client, monkeypatch):
    r = _recipe("Riz", id="r1", nature="Ingrédient")
    for k in ("tags", "note", "etat"):
        r.pop(k)
    _catalogue(monkeypatch, [r])
    [item] = client.get("/api/catalogue").json()
    assert item["tags"] == [] and item["note"] == "" and item["etat"] == ""


def test_duree_et_usage_depuis_la_base(client, monkeypatch):
    poulet = _recipe("Poulet", id="p1")
    soupe = _recipe("Soupe", id="s1")
    _catalogue(monkeypatch, [poulet, soupe])
    asyncio.run(main.db.save_enriched("p1", "Poulet", "[]", cuisson_minutes=25))
    # Planning validé : Poulet midi jour 1 et soir jour 3 → 2 usages, dernier = J3.
    cases = [_case(1, "midi", poulet, persons=2), _case(3, "soir", poulet, persons=2)]
    r = client.post("/construire", data={"week_start": "2026-03-02", "meals": json.dumps(cases)},
                    follow_redirects=False)
    assert r.status_code == 303
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    client.post(f"/planning/{pid}/valider")
    data = {x["id"]: x for x in client.get("/api/catalogue").json()}
    assert data["p1"]["duree"] == 25
    assert data["p1"]["usage"] == {"count": 2, "last": "2026-03-04"}
    assert data["s1"]["usage"] == {"count": 0, "last": ""}
    assert data["s1"]["duree"] == 0


def test_brouillon_ne_compte_pas_dans_usage(client, monkeypatch):
    poulet = _recipe("Poulet", id="p1")
    _catalogue(monkeypatch, [poulet])
    client.post("/construire", data={"week_start": "2026-03-02", "action": "brouillon",
                                     "meals": json.dumps([_case(1, "midi", poulet, persons=2)])},
                follow_redirects=False)
    [item] = client.get("/api/catalogue").json()
    assert item["usage"] == {"count": 0, "last": ""}


def test_erreur_notion_renvoie_502(client, monkeypatch):
    async def _boom():
        raise RuntimeError("Notion down")
    monkeypatch.setattr(main.notion, "get_all_recipes", _boom)
    res = client.get("/api/catalogue")
    assert res.status_code == 502
    body = res.json()
    assert isinstance(body, dict) and body["error"]


def test_cache_local_en_echec_ne_bloque_pas(client, monkeypatch):
    _catalogue(monkeypatch, [_recipe("Curry", id="c1")])

    async def _boom(*a, **k):
        raise RuntimeError("sqlite KO")
    monkeypatch.setattr(main.db, "get_all_enriched_durations", _boom)
    monkeypatch.setattr(main.db, "get_recipe_usage", _boom)
    res = client.get("/api/catalogue")
    assert res.status_code == 200
    [item] = res.json()
    assert item["duree"] == 0 and item["usage"] == {"count": 0, "last": ""}


def test_gabarit_constructeur(client):
    html = client.get("/").text
    assert 'id="pk-extra"' in html                 # filtres rapides du picker
    assert "/static/js/builder-picker.js" in html


def test_js_expose_les_fonctions_pures():
    """Les fonctions pures ajoutées par L12 sont exposées pour L16."""
    from tests.test_static_js import STATIC
    src = (STATIC / "js" / "builder.js").read_text(encoding="utf-8")
    exposed = src.split("window.Builder = {", 1)[1].split("};", 1)[0]
    for fn in ("snapshot", "stepPersons", "freeGroup", "detachMidi", "mealTargets", "copyMeal",
               "moveMeal", "leftoversTarget", "applyLeftovers", "seasonOf", "noteValue",
               "recencyLabel", "filterCatalogue", "sortCatalogue", "EXTRA_FILTERS", "PICKER_PAGE"):
        assert fn in exposed, fn
    picker = (STATIC / "js" / "builder-picker.js").read_text(encoding="utf-8")
    assert "Réessayer" in picker and "Chargement du catalogue" in picker
    assert "setTimeout" in picker                   # recherche temporisée
    assert "card.type = 'button'" in picker         # cartes en <button>
