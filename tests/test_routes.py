"""Tests des routes FastAPI via TestClient, avec Notion/LLM simulés (sans réseau)."""

import json

import pytest
from starlette.testclient import TestClient

import app.main as main


def _recipe(nom, repas="Plat", **kw):
    return {
        "id": kw.get("id", nom.lower()), "nom": nom, "url": kw.get("url", ""),
        "notion_url": "", "repas": repas, "tags": kw.get("tags", []),
        "note": "", "etat": kw.get("etat", ""), "moment": kw.get("moment", ""),
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbpath = str(tmp_path / "t.db")
    main.db.path = dbpath
    # generate_shopping écrit via settings.database_path : l'aligner aussi.
    monkeypatch.setattr(main.settings, "database_path", dbpath)

    async def _noop(*a, **k):
        return True
    monkeypatch.setattr(main.notion, "ensure_ingredients_field", _noop)
    with TestClient(main.app) as c:
        yield c


def test_index_and_historique(client):
    assert client.get("/").status_code == 200
    assert client.get("/historique").status_code == 200
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/sw.js").status_code == 200
    assert client.get("/static/manifest.json").status_code == 200


def test_recettes_lists(client, monkeypatch):
    async def _all():
        return [_recipe("Tarte", "Dessert", tags=["Fun"]), _recipe("Curry")]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    r = client.get("/recettes")
    assert r.status_code == 200 and "Tarte" in r.text and "Curry" in r.text


def test_rate_valid_and_invalid(client, monkeypatch):
    async def _rate(pid, note):
        return {}
    monkeypatch.setattr(main.notion, "update_rating", _rate)
    assert client.post("/api/rate/x", json={"note": "⭐⭐"}).json().get("success")
    assert client.post("/api/rate/x", json={"note": ""}).json().get("success")  # effacer
    assert "error" in client.post("/api/rate/x", json={"note": "pas une note"}).json()


def test_analyze_url(client, monkeypatch):
    async def _extract(url):
        return {"nom": "Lasagnes", "type_repas": "Plat", "tags": ["Viande"],
                "ingredients": ["500 g boeuf", "lasagnes"], "instructions": "Cuire.",
                "image_url": "http://img", "source": "jsonld"}
    monkeypatch.setattr(main.llm, "extract_recipe_from_url", _extract)
    d = client.post("/api/analyze-url", json={"url": "http://x"}).json()
    assert d["nom"] == "Lasagnes"
    assert d["ingredients"] == "500 g boeuf\nlasagnes"   # liste -> texte
    assert d["source"] == "jsonld"
    assert "error" in client.post("/api/analyze-url", json={"url": ""}).json()


def test_generer_success_then_view(client, monkeypatch):
    async def _all():
        return [_recipe("Poulet"), _recipe("Soupe")]
    async def _gen(**kw):
        return [
            {"jour": 1, "moment": "midi", "nom_recette": "Poulet", "type_repas": "Plat"},
            {"jour": 1, "moment": "soir", "nom_recette": "Soupe", "type_repas": "Plat"},
        ]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.llm, "generate_planning", _gen)
    r = client.post("/generer", data={"week_start": "2026-01-05", "saison": "Hiver"},
                    follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert client.get(loc).status_code == 200


def test_generer_error_preserves_form(client, monkeypatch):
    async def _boom():
        raise RuntimeError("Notion down")
    monkeypatch.setattr(main.notion, "get_all_recipes", _boom)
    r = client.post("/generer", data={
        "week_start": "2026-01-05", "saison": "Été", "ingredients_force": "courgettes",
        "custom_prompt": "léger le soir",
    })
    assert r.status_code == 200
    # la saisie est repassée au formulaire
    assert "courgettes" in r.text and "léger le soir" in r.text


def test_update_meal_updates_without_duplicate(client, monkeypatch):
    async def _all():
        return [_recipe("Poulet"), _recipe("Soupe"), _recipe("Curry")]
    async def _gen(**kw):
        return [
            {"jour": 1, "moment": "midi", "nom_recette": "Poulet", "type_repas": "Plat"},
            {"jour": 1, "moment": "soir", "nom_recette": "Soupe", "type_repas": "Plat"},
        ]
    async def _ext(nom, url="", nb=4):
        return {"ingredients": [{"nom": "sel", "quantite": "1", "unite": "pincée"}]}
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.llm, "generate_planning", _gen)
    monkeypatch.setattr(main.llm, "extract_ingredients", _ext)

    r = client.post("/generer", data={"week_start": "2026-01-05", "saison": "Hiver"},
                    follow_redirects=False)
    pid = int(r.headers["location"].rsplit("/", 1)[1])

    resp = client.post(f"/api/update-meal/{pid}",
                       json={"jour": 1, "moment": "midi", "nouvelle_recette": "Curry"})
    assert resp.json().get("success")
    # le repas est remplacé et AUCUN planning dupliqué n'est créé
    import asyncio
    asyncio.run(main.db.mark_planning_valid(pid))
    plannings = asyncio.run(main.db.list_plannings(limit=10))
    assert len(plannings) == 1
    p = asyncio.run(main.db.get_planning_with_recipes(pid))
    assert {r["recipe_name"] for r in p["recipes"]} == {"Curry", "Soupe"}


def test_planning_draft_then_validate(client, monkeypatch):
    async def _all():
        return [_recipe("Poulet"), _recipe("Soupe")]
    async def _gen(**kw):
        return [
            {"jour": 1, "moment": "midi", "nom_recette": "Poulet", "type_repas": "Plat"},
            {"jour": 1, "moment": "soir", "nom_recette": "Soupe", "type_repas": "Plat"},
        ]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.llm, "generate_planning", _gen)
    pid = int(client.post("/generer", data={"week_start": "2026-01-05", "saison": "Hiver"},
                          follow_redirects=False).headers["location"].rsplit("/", 1)[1])
    # brouillon : pas dans l'historique, bandeau de validation présent
    assert "Semaine du" not in client.get("/historique").text
    assert "Valider la semaine" in client.get(f"/planning/{pid}").text
    # validation
    assert client.post(f"/planning/{pid}/valider").json().get("success")
    assert "Semaine du" in client.get("/historique").text
    assert "Semaine validée" in client.get(f"/planning/{pid}").text


def test_detail_recette(client, monkeypatch):
    async def _get(pid):
        return _recipe("Tarte", "Dessert", id="abc") if pid == "abc" else None
    async def _instr(pid):
        return ["Préchauffer le four.", "Enfourner 30 min."]
    async def _enriched(nid):
        return {"ingredients": json.dumps([{"nom": "farine", "quantite": "200", "unite": "g"}])}
    monkeypatch.setattr(main.notion, "get_recipe", _get)
    monkeypatch.setattr(main.notion, "get_recipe_instructions", _instr)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)
    r = client.get("/recette/abc")
    assert r.status_code == 200
    assert "Tarte" in r.text
    assert "farine" in r.text                 # ingrédient
    assert "Enfourner 30 min." in r.text      # instruction
    assert 'id="srv-n"' in r.text             # sélecteur de portions
    # recette inconnue -> 404
    assert client.get("/recette/zzz").status_code == 404


def test_ajouter_recette_manuel(client, monkeypatch):
    created = {}
    async def _create(nom, url="", repas="", tags=None, moment=""):
        created["nom"] = nom
        return {"id": "newid", "url": "https://notion/newid"}
    async def _noop(*a, **k):
        return {}
    monkeypatch.setattr(main.notion, "create_recipe", _create)
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)
    monkeypatch.setattr(main.notion, "append_instructions", _noop)
    monkeypatch.setattr(main.notion, "update_image", _noop)
    r = client.post("/ajouter-recette", data={
        "nom": "Gratin", "repas": "Plat", "ingredients_manual": "200 g pommes de terre\ncrème",
        "steps": ["Éplucher.", "Cuire au four."], "image_url": "http://img",
    })
    assert r.status_code == 200 and "succès" in r.text
    assert created["nom"] == "Gratin"


def test_alternatives_and_shopping(client, monkeypatch):
    async def _all():
        return [_recipe("Poulet"), _recipe("Soupe"), _recipe("Curry")]
    async def _gen(**kw):
        return [
            {"jour": 1, "moment": "midi", "nom_recette": "Poulet", "type_repas": "Plat"},
            {"jour": 1, "moment": "soir", "nom_recette": "Soupe", "type_repas": "Plat"},
        ]
    async def _batch(plats, nb):
        return [{"nom": "sel", "quantite": "1", "unite": "pincée"}]
    async def _noop(*a, **k):
        return {}
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.llm, "generate_planning", _gen)
    monkeypatch.setattr(main.llm, "batch_extract_ingredients", _batch)
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)

    pid = int(client.post("/generer", data={"week_start": "2026-01-05", "saison": "Hiver"},
                          follow_redirects=False).headers["location"].rsplit("/", 1)[1])

    alts = client.get(f"/api/alternatives/{pid}").json()["alternatives"]
    assert any(a["nom"] == "Curry" for a in alts)

    shop = client.post(f"/generate-shopping/{pid}").json()
    assert shop.get("success") and shop["liste_courses"][0]["nom"] == "sel"


def test_enrich_one(client, monkeypatch):
    async def _get(pid):
        return _recipe("Curry", id="abc", url="http://r") if pid == "abc" else None
    async def _ext(nom, url="", nb=4):
        return {"ingredients": [{"nom": "riz", "quantite": "200", "unite": "g"}]}
    async def _save(*a, **k):
        return None
    async def _upd(*a, **k):
        return {}
    monkeypatch.setattr(main.notion, "get_recipe", _get)
    monkeypatch.setattr(main.llm, "extract_ingredients", _ext)
    monkeypatch.setattr(main.db, "save_enriched", _save)
    monkeypatch.setattr(main.notion, "update_ingredients", _upd)
    d = client.post("/api/enrich/abc").json()
    assert d.get("success") and d["count"] == 1
    assert "error" in client.post("/api/enrich/zzz").json()  # introuvable


def test_enrichir_page_and_submit(client, monkeypatch):
    async def _get(pid):
        return _recipe("Curry", id="abc", url="http://r")
    async def _extract(url):
        # source : ingrédients bruts + instructions
        return {"ingredients": ["200 g riz", "1 oignon"], "instructions": "Cuire le riz."}
    async def _enriched(nid):
        return {"ingredients": json.dumps([{"nom": "riz", "quantite": "200", "unite": "g"}])}
    async def _instr(pid):
        return ["Cuire le riz."]
    monkeypatch.setattr(main.notion, "get_recipe", _get)
    monkeypatch.setattr(main.llm, "extract_recipe_from_url", _extract)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)
    monkeypatch.setattr(main.notion, "get_recipe_instructions", _instr)
    # étape 1 : page pré-remplie
    page = client.get("/recette/abc/enrichir")
    assert page.status_code == 200
    assert "riz" in page.text and "Cuire le riz." in page.text
    assert "Valider" in page.text

    # étape 2 : validation
    async def _noop(*a, **k):
        return {}
    async def _save(*a, **k):
        return None
    monkeypatch.setattr(main.db, "save_enriched", _save)
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)
    monkeypatch.setattr(main.notion, "rewrite_recipe_body", _noop)
    monkeypatch.setattr(main.notion, "update_image", _noop)
    monkeypatch.setattr(main.notion, "update_recipe_meta", _noop)
    r = client.post("/recette/abc/enrichir", data={
        "repas": "Plat", "ingredients_text": "200 g riz\n1 oignon",
        "steps": ["Cuire le riz.", "Ajouter l'oignon."],
    }, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/recette/abc"


def test_enrich_all(client, monkeypatch):
    async def _all():
        return [_recipe("Poulet", id="p1", url="http://r")]
    async def _enriched(nid):
        return None
    async def _ext(nom, url="", nb=4):
        return {"ingredients": [{"nom": "poulet", "quantite": "1", "unite": "kg"}]}
    async def _save(*a, **k):
        return None
    async def _upd(*a, **k):
        return {}
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)
    monkeypatch.setattr(main.db, "save_enriched", _save)
    monkeypatch.setattr(main.llm, "extract_ingredients", _ext)
    monkeypatch.setattr(main.notion, "update_ingredients", _upd)
    d = client.post("/api/enrich-all").json()
    assert d.get("success") and d["enriched"] == 1


def test_update_meal_invalid_params(client):
    # Validation : jour hors [1,7] ou moment invalide -> erreur sans toucher la base
    bad = client.post("/api/update-meal/1", json={"jour": 999, "moment": "xyz", "nouvelle_recette": "X"}).json()
    assert "error" in bad
    bad2 = client.post("/api/update-meal/1", json={"jour": 1, "moment": "midi", "nouvelle_recette": ""}).json()
    assert "error" in bad2


def test_update_side_invalid_params(client):
    bad = client.post("/api/update-side/1", json={"jour": 0, "moment": "midi"}).json()
    assert "error" in bad
    bad2 = client.post("/api/update-side/1", json={"jour": 3, "moment": "nope"}).json()
    assert "error" in bad2


def test_enrich_all_stream(client, monkeypatch):
    async def _all():
        return [_recipe("Poulet", id="p1", url="http://r"), _recipe("SansUrl", id="p2")]
    async def _enriched(nid):
        return None
    async def _ext(nom, url="", nb=4):
        return {"ingredients": [{"nom": "poulet", "quantite": "1", "unite": "kg"}]}
    async def _save(*a, **k):
        return None
    async def _upd(*a, **k):
        return {}
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)
    monkeypatch.setattr(main.db, "save_enriched", _save)
    monkeypatch.setattr(main.llm, "extract_ingredients", _ext)
    monkeypatch.setattr(main.notion, "update_ingredients", _upd)
    r = client.get("/api/enrich-all/stream")
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    body = r.text
    assert "event: done" in body
    assert '"enriched": 1' in body  # Poulet enrichi, SansUrl ignoré


def test_dupliquer_planning(client, monkeypatch):
    # Crée un planning validé, le duplique -> nouveau brouillon avec plats copiés
    async def _all():
        return [_recipe("Poulet")]
    async def _gen(**kw):
        return [{"jour": 1, "moment": "midi", "nom_recette": "Poulet", "type_repas": "Plat"}]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.llm, "generate_planning", _gen)
    pid = int(client.post("/generer", data={"week_start": "2026-01-05", "saison": "Hiver"},
                          follow_redirects=False).headers["location"].rsplit("/", 1)[1])
    d = client.post(f"/planning/{pid}/dupliquer").json()
    assert d["success"] and d["planning_id"] != pid
    # la copie est un brouillon (bandeau de validation présent)
    assert "Valider la semaine" in client.get(f"/planning/{d['planning_id']}").text
