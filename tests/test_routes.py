"""Tests des routes FastAPI via TestClient, avec Notion/LLM simulés (sans réseau)."""

import json

import pytest
from starlette.testclient import TestClient

import app.main as main


def _recipe(nom, repas="Plat", **kw):
    # repas est désormais une liste de types (multi_select). On accepte une
    # string en entrée par confort et on la normalise en liste.
    if isinstance(repas, str):
        repas = [repas] if repas else []
    return {
        "id": kw.get("id", nom.lower()), "nom": nom, "url": kw.get("url", ""),
        "notion_url": "", "repas": repas, "tags": kw.get("tags", []),
        "base": kw.get("base", []), "nature": kw.get("nature", "Recette"),
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
    assert 'data-filter="nature"' in r.text          # filtre Nature présent
    assert 'data-nature="Recette"' in r.text          # nature portée par les cartes


def test_rate_valid_and_invalid(client, monkeypatch):
    status = {"calls": []}
    async def _rate(pid, note):
        return {}
    async def _status(pid, etat):
        status["calls"].append(etat)
        return {}
    monkeypatch.setattr(main.notion, "update_rating", _rate)
    monkeypatch.setattr(main.notion, "update_status", _status)
    assert client.post("/api/rate/x", json={"note": "⭐⭐"}).json().get("success")
    assert status["calls"] == ["Testée"]                          # notée → Testée
    assert client.post("/api/rate/x", json={"note": ""}).json().get("success")  # effacer
    assert status["calls"] == ["Testée"]                          # effacer ne re-valide pas
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


def test_analyze_text(client, monkeypatch):
    async def _extract(text):
        return {"nom": "Curry", "type_repas": "Plat", "tags": ["Épicé"],
                "ingredients": ["2 oignons", "riz"], "instructions": "Cuire.\nServir.",
                "image_url": "", "source": "llm-text"}
    monkeypatch.setattr(main.llm, "extract_recipe_from_text", _extract)
    d = client.post("/api/analyze-text", json={"text": "un curry..."}).json()
    assert d["nom"] == "Curry"
    assert d["ingredients"] == "2 oignons\nriz"   # liste -> texte
    assert d["source"] == "llm-text"
    assert "error" in client.post("/api/analyze-text", json={"text": "  "}).json()


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
    assert "recipe-stars" in r.text           # widget de notation sur la fiche
    assert 'id="srv-n"' in r.text             # sélecteur de portions
    # recette inconnue -> 404
    assert client.get("/recette/zzz").status_code == 404


def test_ajouter_recette_manuel(client, monkeypatch):
    created = {}
    async def _create(nom, url="", repas="", tags=None, moment="", nature="Recette", base=None):
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


def test_ajouter_recette_types_multiples(client, monkeypatch):
    # Deux types cochés -> create_recipe reçoit la liste des deux.
    created = {}
    async def _create(nom, url="", repas="", tags=None, moment="", nature="Recette", base=None):
        created["repas"] = repas
        return {"id": "newid", "url": "https://notion/newid"}
    async def _noop(*a, **k):
        return {}
    monkeypatch.setattr(main.notion, "create_recipe", _create)
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)
    monkeypatch.setattr(main.notion, "append_instructions", _noop)
    monkeypatch.setattr(main.notion, "update_image", _noop)
    r = client.post("/ajouter-recette", data={
        "nom": "Cookie", "repas": ["Goûter", "Dessert"],
        "ingredients_manual": "200 g farine",
    })
    assert r.status_code == 200 and "succès" in r.text
    assert created["repas"] == ["Goûter", "Dessert"]


def test_ajouter_recette_ecrit_base(client, monkeypatch):
    # Les cases Base cochées -> create_recipe reçoit la liste des bases.
    created = {}
    async def _create(nom, url="", repas="", tags=None, moment="", nature="Recette", base=None):
        created["base"] = base
        created["nature"] = nature
        return {"id": "newid", "url": "https://notion/newid"}
    async def _noop(*a, **k):
        return {}
    monkeypatch.setattr(main.notion, "create_recipe", _create)
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)
    monkeypatch.setattr(main.notion, "append_instructions", _noop)
    monkeypatch.setattr(main.notion, "update_image", _noop)
    r = client.post("/ajouter-recette", data={
        "nom": "Saumon grillé", "repas": "Plat", "base": ["Poisson", "Légume"],
        "ingredients_manual": "1 pavé de saumon",
    })
    assert r.status_code == 200 and "succès" in r.text
    assert created["base"] == ["Poisson", "Légume"]
    assert created["nature"] == "Recette"


def test_enrichir_submit_ecrit_base(client, monkeypatch):
    # Les cases Base cochées -> update_recipe_meta reçoit la liste des bases.
    async def _get(pid):
        return _recipe("Saumon", id="abc", url="http://r")
    async def _enriched(nid):
        return None
    async def _instr(pid):
        return []
    meta = {}
    async def _meta(page_id, repas="", tags=None, nature="Recette", base=None):
        meta["base"] = base
        meta["nature"] = nature
        return {}
    async def _noop(*a, **k):
        return {}
    async def _save(*a, **k):
        return None
    monkeypatch.setattr(main.notion, "get_recipe", _get)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)
    monkeypatch.setattr(main.notion, "get_recipe_instructions", _instr)
    monkeypatch.setattr(main.notion, "update_recipe_meta", _meta)
    monkeypatch.setattr(main.db, "save_enriched", _save)
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)
    monkeypatch.setattr(main.notion, "rewrite_recipe_body", _noop)
    monkeypatch.setattr(main.notion, "update_image", _noop)
    r = client.post("/recette/abc/enrichir", data={
        "repas": "Plat", "base": ["Poisson"], "ingredients_text": "1 pavé de saumon",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert meta["base"] == ["Poisson"]
    assert meta["nature"] == "Recette"


def test_alternatives_and_shopping(client, monkeypatch):
    async def _all():
        return [_recipe("Poulet"), _recipe("Soupe"), _recipe("Curry")]
    async def _gen(**kw):
        return [
            {"jour": 1, "moment": "midi", "nom_recette": "Poulet", "type_repas": "Plat"},
            {"jour": 1, "moment": "soir", "nom_recette": "Soupe", "type_repas": "Plat"},
        ]
    # Liste de courses ANCRÉE sur le cache DB (plus de batch LLM à l'aveugle).
    async def _enriched(nid):
        return {"ingredients": json.dumps([{"nom": "riz", "quantite": "100", "unite": "g"}])}
    async def _noop(*a, **k):
        return {}
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.llm, "generate_planning", _gen)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)

    pid = int(client.post("/generer", data={"week_start": "2026-01-05", "saison": "Hiver",
                                             "nb_lun": "8"},
                          follow_redirects=False).headers["location"].rsplit("/", 1)[1])

    alts = client.get(f"/api/alternatives/{pid}").json()["alternatives"]
    assert any(a["nom"] == "Curry" for a in alts)

    shop = client.post(f"/generate-shopping/{pid}").json()
    assert shop.get("success")
    riz = next(i for i in shop["liste_courses"] if i["nom"] == "riz")
    # 2 repas lundi (8 pers) × 100 g / base 4 = 400 g, avec recettes source.
    assert riz["quantite"] == "400"
    assert set(riz["recettes"]) == {"Poulet", "Soupe"}


def _make_shopping_planning(client, monkeypatch):
    """Crée un planning avec une liste de courses (riz) et renvoie son id."""
    async def _all():
        return [_recipe("Poulet"), _recipe("Soupe")]
    async def _gen(**kw):
        return [
            {"jour": 1, "moment": "midi", "nom_recette": "Poulet", "type_repas": "Plat"},
            {"jour": 1, "moment": "soir", "nom_recette": "Soupe", "type_repas": "Plat"},
        ]
    async def _enriched(nid):
        return {"ingredients": json.dumps([{"nom": "riz", "quantite": "100", "unite": "g"}])}
    async def _noop(*a, **k):
        return {}
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.llm, "generate_planning", _gen)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)
    pid = int(client.post("/generer", data={"week_start": "2026-01-05", "saison": "Hiver"},
                          follow_redirects=False).headers["location"].rsplit("/", 1)[1])
    assert client.post(f"/generate-shopping/{pid}").json().get("success")
    return pid


def test_shopping_check_persists_state(client, monkeypatch):
    import asyncio
    pid = _make_shopping_planning(client, monkeypatch)
    # coche "riz" -> persisté dans courses_checked
    r = client.post(f"/api/shopping-check/{pid}", json={"item": "riz", "checked": True})
    assert r.json().get("success")
    p = asyncio.run(main.db.get_planning_with_recipes(pid))
    assert "riz" in json.loads(p["data_json"]).get("courses_checked", [])
    # décoche -> retiré
    assert client.post(f"/api/shopping-check/{pid}", json={"item": "riz", "checked": False}).json().get("success")
    p = asyncio.run(main.db.get_planning_with_recipes(pid))
    assert "riz" not in json.loads(p["data_json"]).get("courses_checked", [])
    # planning introuvable
    assert "error" in client.post("/api/shopping-check/999999", json={"item": "x", "checked": True}).json()


def test_enrich_week(client, monkeypatch):
    pid = _make_shopping_planning(client, monkeypatch)
    d = client.post(f"/api/enrich-week/{pid}").json()
    # 2 recettes du planning (Poulet, Soupe), enrichies depuis le cache
    assert d.get("success") and d["total"] == 2 and d["enriched"] == 2
    # planning introuvable
    assert "error" in client.post("/api/enrich-week/999999").json()


def test_partager_et_courses_public(client, monkeypatch):
    pid = _make_shopping_planning(client, monkeypatch)
    # partager -> URL /courses/<token>
    d = client.post(f"/planning/{pid}/partager").json()
    assert d.get("url", "").startswith("/courses/")
    # idempotent : même token au 2e appel
    assert client.post(f"/planning/{pid}/partager").json()["url"] == d["url"]

    # page publique : 200 et affiche le riz (non coché)
    page = client.get(d["url"])
    assert page.status_code == 200
    assert "riz" in page.text.lower()

    # une fois coché, l'item disparaît de la page publique
    client.post(f"/api/shopping-check/{pid}", json={"item": "riz", "checked": True})
    page2 = client.get(d["url"])
    assert page2.status_code == 200
    assert "Riz" not in page2.text

    # token inconnu -> 404
    assert client.get("/courses/inexistant").status_code == 404


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


def test_enrichir_submit_types_multiples(client, monkeypatch):
    # Deux types cochés (multi_select) -> update_recipe_meta reçoit la liste.
    async def _get(pid):
        return _recipe("Cookie", id="abc", url="http://r")
    async def _enriched(nid):
        return None
    async def _instr(pid):
        return []
    meta = {}
    async def _meta(page_id, repas="", tags=None, nature="Recette", base=None):
        meta["repas"] = repas
        meta["base"] = base
        return {}
    async def _noop(*a, **k):
        return {}
    async def _save(*a, **k):
        return None
    monkeypatch.setattr(main.notion, "get_recipe", _get)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)
    monkeypatch.setattr(main.notion, "get_recipe_instructions", _instr)
    monkeypatch.setattr(main.notion, "update_recipe_meta", _meta)
    monkeypatch.setattr(main.db, "save_enriched", _save)
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)
    monkeypatch.setattr(main.notion, "rewrite_recipe_body", _noop)
    monkeypatch.setattr(main.notion, "update_image", _noop)
    r = client.post("/recette/abc/enrichir", data={
        "repas": ["Goûter", "Dessert"], "ingredients_text": "200 g farine",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert meta["repas"] == ["Goûter", "Dessert"]


def test_enrichir_renomme_librement(client, monkeypatch):
    # Un nom soumis différent du nom actuel doit renommer la recette dans Notion.
    async def _get(pid):
        return _recipe("Curry", id="abc", url="http://r")
    async def _extract(url):
        return {"ingredients": ["200 g riz"], "instructions": "Cuire."}
    async def _enriched(nid):
        return None
    async def _instr(pid):
        return []
    renamed = {}
    saved = {}
    async def _title(pid, nom):
        renamed["nom"] = nom
        return {}
    async def _save(nid, name, *a, **k):
        saved["name"] = name
        return None
    async def _noop(*a, **k):
        return {}
    monkeypatch.setattr(main.notion, "get_recipe", _get)
    monkeypatch.setattr(main.llm, "extract_recipe_from_url", _extract)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)
    monkeypatch.setattr(main.notion, "get_recipe_instructions", _instr)
    monkeypatch.setattr(main.notion, "update_recipe_title", _title)
    monkeypatch.setattr(main.db, "save_enriched", _save)
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)
    monkeypatch.setattr(main.notion, "rewrite_recipe_body", _noop)
    monkeypatch.setattr(main.notion, "update_image", _noop)
    monkeypatch.setattr(main.notion, "update_recipe_meta", _noop)
    r = client.post("/recette/abc/enrichir", data={
        "nom": "Curry de légumes", "repas": "Plat",
        "ingredients_text": "200 g riz",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert renamed.get("nom") == "Curry de légumes"       # renommé dans Notion
    assert saved.get("name") == "Curry de légumes"        # cache à jour


def test_supprimer_recette(client, monkeypatch):
    archived = {}
    purged = {}
    async def _archive(pid):
        archived["id"] = pid
        return {"archived": True}
    async def _delete(nid):
        purged["id"] = nid
    monkeypatch.setattr(main.notion, "archive_recipe", _archive)
    monkeypatch.setattr(main.db, "delete_enriched", _delete)
    d = client.post("/recette/abc/supprimer").json()
    assert d.get("success")
    assert archived.get("id") == "abc" and purged.get("id") == "abc"


def test_supprimer_recette_erreur(client, monkeypatch):
    async def _boom(pid):
        raise RuntimeError("Notion down")
    monkeypatch.setattr(main.notion, "archive_recipe", _boom)
    d = client.post("/recette/abc/supprimer").json()
    assert "error" in d


def test_enrichir_demande_url_si_absente(client, monkeypatch):
    # recette SANS url -> bannière + champ URL ; soumettre une URL la persiste
    async def _get(pid):
        return _recipe("Maison", id="abc", url="")
    async def _enriched(nid):
        return None
    async def _instr(pid):
        return []
    monkeypatch.setattr(main.notion, "get_recipe", _get)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)
    monkeypatch.setattr(main.notion, "get_recipe_instructions", _instr)
    page = client.get("/recette/abc/enrichir")
    assert page.status_code == 200 and "n'a pas d'URL source" in page.text

    saved = {}
    async def _noop(*a, **k):
        return {}
    async def _save(*a, **k):
        return None
    async def _url(pid, url):
        saved["url"] = url
        return {}
    monkeypatch.setattr(main.db, "save_enriched", _save)
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)
    monkeypatch.setattr(main.notion, "rewrite_recipe_body", _noop)
    monkeypatch.setattr(main.notion, "update_image", _noop)
    monkeypatch.setattr(main.notion, "update_recipe_meta", _noop)
    monkeypatch.setattr(main.notion, "update_recipe_url", _url)
    r = client.post("/recette/abc/enrichir", data={
        "repas": "Plat", "source_url": "https://example.com/recette",
        "ingredients_text": "200 g riz",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert saved.get("url") == "https://example.com/recette"


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


def test_planning_warns_non_enrichi(client, monkeypatch):
    import json
    async def _all():
        return [_recipe("Poulet", id="n1")]
    async def _gen(**kw):
        return [{"jour": 1, "moment": "midi", "nom_recette": "Poulet",
                 "notion_id": "n1", "accompagnement": None}]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.llm, "generate_planning", _gen)
    pid = int(client.post("/generer", data={"week_start": "2026-01-05", "saison": "Hiver"},
                          follow_redirects=False).headers["location"].rsplit("/", 1)[1])
    # recette non enrichie -> avertissement présent
    assert "À compléter" in client.get(f"/planning/{pid}").text
    # après enrichissement (ingrédients en cache) -> plus d'avertissement
    import asyncio
    asyncio.run(main.db.save_enriched("n1", "Poulet", ingredients=json.dumps(
        [{"nom": "g de poulet", "quantite": "200", "unite": ""}])))
    assert "À compléter" not in client.get(f"/planning/{pid}").text


def test_planning_repas_string_legacy_rendered_as_one_chip(client, monkeypatch):
    """Ancien planning : « repas » stocké en string → coercé en liste à l'affichage
    (sinon la boucle de chips l'éclaterait lettre par lettre : « P l a t »)."""
    import asyncio, json
    data = {"plats": [{"jour": 1, "moment": "midi", "nom_recette": "Truc maison",
                       "repas": "Plat", "notion_id": "", "accompagnement": None}],
            "liste_courses": [], "per_day": "4,4,4,4,4,4,4"}
    pid = asyncio.run(main.db.save_planning(
        week_start="2026-01-05", saison="Hiver", nb_personnes=4, ingredients_force="",
        data_json=json.dumps(data, ensure_ascii=False),
        recipes=[{"notion_id": "", "recipe_name": "Truc maison", "repas_type": "Plat",
                  "jour": 1, "moment": "midi"}],
    ))
    html = client.get(f"/planning/{pid}").text
    assert '<span class="chip chip-repas">Plat</span>' in html
    assert '<span class="chip chip-repas">P</span>' not in html


def test_parse_off_meals():
    assert main._parse_off_meals("1:midi,6:soir") == {(1, "midi"), (6, "soir")}
    assert main._parse_off_meals("") == set()
    # jetons invalides ignorés
    assert main._parse_off_meals("9:midi,3:brunch,x:soir,2:soir") == {(2, "soir")}


def test_generer_desactive_des_repas(client, monkeypatch):
    async def _all():
        return [_recipe("Poulet", id="p1")]
    captured = {}
    async def _gen(**kw):
        captured["off"] = kw.get("off_meals")
        # respecte les créneaux off comme le vrai code
        off = kw.get("off_meals") or set()
        return [{"jour": j, "moment": m, "nom_recette": "Poulet", "notion_id": "p1",
                 "accompagnement": None}
                for j in range(1, 8) for m in ("midi", "soir") if (j, m) not in off]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.llm, "generate_planning", _gen)
    loc = client.post("/generer", data={"week_start": "2026-01-05", "saison": "Hiver",
                                         "off_meals": "1:midi,7:soir"},
                      follow_redirects=False).headers["location"]
    pid = int(loc.rsplit("/", 1)[1])
    assert captured["off"] == {(1, "midi"), (7, "soir")}
    page = client.get(f"/planning/{pid}").text
    assert "Absent" in page  # placeholder rendu


def test_week_nutrition(client):
    import asyncio, json
    async def run():
        await main.db.save_enriched("n1", "Pâtes", ingredients=json.dumps([
            {"nom": "g de pâtes", "quantite": "200", "unite": ""}]))
        plats = [{"jour": j, "moment": m, "nom_recette": "Pâtes", "notion_id": "n1",
                  "accompagnement": None} for j in range(1, 8) for m in ("midi", "soir")]
        nut = await main._week_nutrition(plats)
        assert nut and nut["calories"] > 0 and nut["meals_total"] == 14
        # détail par jour : 7 jours, chacun avec midi+soir+total estimés
        assert len(nut["par_jour"]) == 7
        d0 = nut["par_jour"][0]
        assert d0["nom"] == "Lundi" and d0["midi"] and d0["soir"]
        assert d0["total"]["calories"] == d0["midi"]["calories"] + d0["soir"]["calories"]
        # aucune recette estimable -> None
        assert await main._week_nutrition([{"jour": 1, "moment": "midi", "nom_recette": "X",
                                            "notion_id": "zzz", "accompagnement": None}]) is None
    asyncio.run(run())


def test_enrichir_submit_notion_echec(client, monkeypatch):
    """Une écriture Notion qui lève → pas de redirection succès : on re-affiche
    le formulaire avec la saisie et un message d'erreur précis."""
    async def _get(pid):
        return _recipe("Curry", id="abc", url="http://r")
    async def _noop(*a, **k):
        return {}
    async def _save(*a, **k):
        return None
    async def _boom(*a, **k):
        raise RuntimeError("Notion 500")
    monkeypatch.setattr(main.notion, "get_recipe", _get)
    monkeypatch.setattr(main.db, "save_enriched", _save)          # cache local OK
    monkeypatch.setattr(main.notion, "update_ingredients", _boom)  # Notion KO
    monkeypatch.setattr(main.notion, "rewrite_recipe_body", _noop)
    monkeypatch.setattr(main.notion, "update_image", _noop)
    monkeypatch.setattr(main.notion, "update_recipe_meta", _noop)

    r = client.post("/recette/abc/enrichir", data={
        "repas": "Plat", "ingredients_text": "200 g riz\n1 oignon",
        "steps": ["Cuire le riz."],
    }, follow_redirects=False)
    # PAS une redirection 303 de succès
    assert r.status_code == 200
    # message d'erreur précis listant l'étape ratée + sort du cache local
    assert "enregistrement dans Notion" in r.text
    assert "ingrédients" in r.text
    assert "cache local des ingrédients a bien été enregistré" in r.text
    # la saisie est conservée (ré-affichée)
    assert "riz" in r.text and "Cuire le riz." in r.text


def test_ajouter_recette_echec_ingredients(client, monkeypatch):
    """Échec de save ingrédients → error listant l'étape ; page créée signalée,
    pas de « succès » nu."""
    async def _create(nom, url="", repas="", tags=None, moment="", nature="Recette", base=None):
        return {"id": "newid", "url": "https://notion/newid"}
    async def _noop(*a, **k):
        return {}
    async def _boom(*a, **k):
        raise RuntimeError("Notion KO")
    monkeypatch.setattr(main.notion, "create_recipe", _create)
    monkeypatch.setattr(main.notion, "update_ingredients", _boom)
    monkeypatch.setattr(main.notion, "append_instructions", _noop)
    monkeypatch.setattr(main.notion, "update_image", _noop)

    r = client.post("/ajouter-recette", data={
        "nom": "Gratin", "repas": "Plat",
        "ingredients_manual": "200 g pommes de terre\ncrème",
    })
    assert r.status_code == 200
    assert "créée dans Notion" in r.text
    assert "les ingrédients" in r.text
    # pas de succès trompeur
    assert "ajoutée avec succès" not in r.text


def test_generer_valide_recette_inventee(client, monkeypatch):
    """Le LLM invente une recette absente du catalogue → le créneau est re-tiré
    avec une recette réelle du catalogue (ou listé en non-résolu)."""
    async def _all():
        return [_recipe("Poulet"), _recipe("Soupe")]
    async def _gen(**kw):
        return [
            {"jour": 1, "moment": "midi", "nom_recette": "Inventée", "type_repas": "Plat"},
            {"jour": 1, "moment": "soir", "nom_recette": "Poulet", "type_repas": "Plat"},
        ]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.llm, "generate_planning", _gen)
    r = client.post("/generer", data={"week_start": "2026-01-05", "saison": "Hiver"},
                    follow_redirects=False)
    assert r.status_code == 303
    page = client.get(r.headers["location"]).text
    # la recette inventée a été remplacée par une recette réelle du catalogue
    assert "Inventée" not in page
    assert "Soupe" in page


def test_free_meal_creates_and_places(client, monkeypatch):
    """Un « repas libre » crée une recette minimale, la place dans le bon créneau
    et régénère une liste de courses incluant les ingrédients saisis."""
    import asyncio

    async def _all():
        return [_recipe("Poulet"), _recipe("Soupe")]
    async def _gen(**kw):
        return [
            {"jour": 1, "moment": "midi", "nom_recette": "Poulet", "type_repas": "Plat"},
            {"jour": 1, "moment": "soir", "nom_recette": "Soupe", "type_repas": "Plat"},
        ]

    created = {}
    async def _create(nom, url="", repas="", tags=None, etat="À essayer", moment="", nature="Recette", base=None):
        created["nom"] = nom
        created["repas"] = repas
        return {"id": "free1", "url": "https://notion.so/free1"}
    ingredients_ecrits = {}
    async def _upd_ings(page_id, text):
        ingredients_ecrits["page_id"] = page_id
        ingredients_ecrits["text"] = text
        return {}

    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.llm, "generate_planning", _gen)
    monkeypatch.setattr(main.notion, "create_recipe", _create)
    monkeypatch.setattr(main.notion, "update_ingredients", _upd_ings)
    # db.save_enriched / get_enriched : vrai cache (round-trip sur la db temporaire)

    pid = int(client.post("/generer", data={"week_start": "2026-01-05", "saison": "Hiver"},
                          follow_redirects=False).headers["location"].rsplit("/", 1)[1])

    resp = client.post(f"/api/free-meal/{pid}", json={
        "jour": 1, "moment": "soir",
        "nom": "Steak + haricots verts",
        "ingredients": "2 steaks\n200 g de haricots verts",
    })
    data = resp.json()
    assert data.get("success")

    # La recette a bien été créée dans Notion (Plat) + ingrédients écrits.
    assert created["nom"] == "Steak + haricots verts"
    assert created["repas"] == "Plat"
    assert ingredients_ecrits["page_id"] == "free1"

    # Le bon créneau (soir) porte la nouvelle recette.
    soir = next(p for p in data["plats"] if p["jour"] == 1 and p["moment"] == "soir")
    assert soir["nom_recette"] == "Steak + haricots verts"
    assert soir["notion_id"] == "free1"
    assert soir["repas"] == ["Plat"]  # type multi-valeurs : liste, pas string

    # La liste de courses inclut les ingrédients saisis.
    noms = {i["nom"] for i in data["liste_courses"]}
    assert "steaks" in noms
    assert "haricots verts" in noms

    # Persisté : la recette figure dans le planning enregistré.
    p = asyncio.run(main.db.get_planning_with_recipes(pid))
    assert "Steak + haricots verts" in {r["recipe_name"] for r in p["recipes"]}


def test_free_meal_creneau_introuvable(client, monkeypatch):
    """Créneau inexistant → erreur, pas de recette créée."""
    async def _all():
        return [_recipe("Poulet")]
    async def _gen(**kw):
        return [{"jour": 1, "moment": "midi", "nom_recette": "Poulet", "type_repas": "Plat"}]
    appels = {"create": 0}
    async def _create(**kw):
        appels["create"] += 1
        return {"id": "x"}
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.llm, "generate_planning", _gen)
    monkeypatch.setattr(main.notion, "create_recipe", _create)

    pid = int(client.post("/generer", data={"week_start": "2026-01-05", "saison": "Hiver"},
                          follow_redirects=False).headers["location"].rsplit("/", 1)[1])
    # jour 1 soir n'existe pas dans ce planning
    resp = client.post(f"/api/free-meal/{pid}", json={
        "jour": 1, "moment": "soir", "nom": "Test", "ingredients": ""})
    assert resp.json().get("error")
    assert appels["create"] == 0  # aucune page Notion orpheline


def _brique_mocks(monkeypatch):
    """Prépare les mocks partagés des tests brique : catalogue Notion minimal,
    génération d'un planning 1 jour, capture de create_recipe/update_ingredients.
    Renvoie (created, ings_ecrits) pour inspection. Le cache DB reste RÉEL."""
    async def _all():
        return [_recipe("Poulet"), _recipe("Soupe")]
    async def _gen(**kw):
        return [
            {"jour": 1, "moment": "midi", "nom_recette": "Poulet", "type_repas": "Plat"},
            {"jour": 1, "moment": "soir", "nom_recette": "Soupe", "type_repas": "Plat"},
        ]
    created = {}
    async def _create(nom, url="", repas="", tags=None, etat="À essayer", moment="", nature="", base=None):
        created["nom"] = nom
        created["nature"] = nature
        created["base"] = base
        created["repas"] = repas
        return {"id": "brique1", "url": "https://notion.so/brique1"}
    ings_ecrits = {}
    async def _upd_ings(page_id, text):
        ings_ecrits["page_id"] = page_id
        ings_ecrits["text"] = text
        return {}
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.llm, "generate_planning", _gen)
    monkeypatch.setattr(main.notion, "create_recipe", _create)
    monkeypatch.setattr(main.notion, "update_ingredients", _upd_ings)
    return created, ings_ecrits


def test_brique_plat_creates_and_places(client, monkeypatch):
    """Quick-create brique en slot plat : crée une recette Nature=Ingrédient +
    Base, avec pour ingrédient elle-même, la place et l'inclut dans les courses."""
    import asyncio
    created, ings_ecrits = _brique_mocks(monkeypatch)

    pid = int(client.post("/generer", data={"week_start": "2026-01-05", "saison": "Hiver"},
                          follow_redirects=False).headers["location"].rsplit("/", 1)[1])

    resp = client.post(f"/api/brique/{pid}", json={
        "jour": 1, "moment": "soir", "slot": "plat",
        "nom": "Riz", "base": ["Féculent"], "quantite": "100", "unite": "g",
    })
    data = resp.json()
    assert data.get("success")

    # create_recipe a bien reçu Nature=Ingrédient + la Base, sans repas forcé.
    assert created["nom"] == "Riz"
    assert created["nature"] == "Ingrédient"
    assert created["base"] == ["Féculent"]
    assert not created["repas"]
    # L'ingrédient écrit = la brique elle-même.
    assert ings_ecrits["page_id"] == "brique1"
    assert "riz" in ings_ecrits["text"].lower()

    # Placée dans le créneau, avec sa nature/base reportées.
    soir = next(p for p in data["plats"] if p["jour"] == 1 and p["moment"] == "soir")
    assert soir["nom_recette"] == "Riz"
    assert soir["notion_id"] == "brique1"
    assert soir["nature"] == "Ingrédient"
    assert soir["base"] == ["Féculent"]

    # Présente dans la liste de courses.
    noms = {i["nom"].lower() for i in data["liste_courses"]}
    assert "riz" in noms

    # Persistée dans le planning enregistré.
    p = asyncio.run(main.db.get_planning_with_recipes(pid))
    assert "Riz" in {r["recipe_name"] for r in p["recipes"]}


def test_brique_accompagnement_creates_and_places(client, monkeypatch):
    """Quick-create brique en slot accompagnement : placée comme side du repas."""
    created, ings_ecrits = _brique_mocks(monkeypatch)

    pid = int(client.post("/generer", data={"week_start": "2026-01-05", "saison": "Hiver"},
                          follow_redirects=False).headers["location"].rsplit("/", 1)[1])

    resp = client.post(f"/api/brique/{pid}", json={
        "jour": 1, "moment": "midi", "slot": "accompagnement",
        "nom": "Brocolis", "base": ["Légume"], "quantite": "200", "unite": "g",
    })
    data = resp.json()
    assert data.get("success")

    assert created["nature"] == "Ingrédient"
    assert created["base"] == ["Légume"]

    # La brique est posée comme accompagnement (pas comme plat).
    midi = next(p for p in data["plats"] if p["jour"] == 1 and p["moment"] == "midi")
    assert midi["nom_recette"] == "Poulet"  # plat inchangé
    acc = midi["accompagnement"]
    assert acc and acc["nom_recette"] == "Brocolis"
    assert acc["notion_id"] == "brique1"
    assert acc["base"] == ["Légume"]

    # Ses ingrédients figurent dans la liste de courses.
    noms = {i["nom"].lower() for i in data["liste_courses"]}
    assert "brocolis" in noms


def test_brique_creneau_introuvable(client, monkeypatch):
    """Créneau inexistant → erreur, aucune recette brique créée."""
    async def _all():
        return [_recipe("Poulet")]
    async def _gen(**kw):
        return [{"jour": 1, "moment": "midi", "nom_recette": "Poulet", "type_repas": "Plat"}]
    appels = {"create": 0}
    async def _create(**kw):
        appels["create"] += 1
        return {"id": "x"}
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.llm, "generate_planning", _gen)
    monkeypatch.setattr(main.notion, "create_recipe", _create)

    pid = int(client.post("/generer", data={"week_start": "2026-01-05", "saison": "Hiver"},
                          follow_redirects=False).headers["location"].rsplit("/", 1)[1])
    # jour 1 soir n'existe pas dans ce planning
    resp = client.post(f"/api/brique/{pid}", json={
        "jour": 1, "moment": "soir", "slot": "plat", "nom": "Riz", "base": ["Féculent"]})
    assert resp.json().get("error")
    assert appels["create"] == 0  # pas de page Notion orpheline
