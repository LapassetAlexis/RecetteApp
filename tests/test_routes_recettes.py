"""Routes des recettes (liste, fiche, ajout, enrichissement, notes), Notion/LLM simulés."""

import json

import app.main as main
from tests.aides import _recipe


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
    async def _create(nom, url="", repas="", tags=None, moment="", nature="Recette", base=None, base_servings=None):
        created["nom"] = nom
        return {"id": "newid", "url": "https://notion/newid"}
    async def _noop(*a, **k):
        return {}
    monkeypatch.setattr(main.notion, "create_recipe", _create)
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)
    monkeypatch.setattr(main.notion, "append_instructions", _noop)
    monkeypatch.setattr(main.notion, "update_image", _noop)
    monkeypatch.setattr(main.notion, "update_portions", _noop)
    r = client.post("/ajouter-recette", data={
        "nom": "Gratin", "repas": "Plat", "ingredients_manual": "200 g pommes de terre\ncrème",
        "steps": ["Éplucher.", "Cuire au four."], "image_url": "http://img",
    })
    assert r.status_code == 200 and "succès" in r.text
    assert created["nom"] == "Gratin"


def test_ajouter_recette_types_multiples(client, monkeypatch):
    # Deux types cochés -> create_recipe reçoit la liste des deux.
    created = {}
    async def _create(nom, url="", repas="", tags=None, moment="", nature="Recette", base=None, base_servings=None):
        created["repas"] = repas
        return {"id": "newid", "url": "https://notion/newid"}
    async def _noop(*a, **k):
        return {}
    monkeypatch.setattr(main.notion, "create_recipe", _create)
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)
    monkeypatch.setattr(main.notion, "append_instructions", _noop)
    monkeypatch.setattr(main.notion, "update_image", _noop)
    monkeypatch.setattr(main.notion, "update_portions", _noop)
    r = client.post("/ajouter-recette", data={
        "nom": "Cookie", "repas": ["Goûter", "Dessert"],
        "ingredients_manual": "200 g farine",
    })
    assert r.status_code == 200 and "succès" in r.text
    assert created["repas"] == ["Goûter", "Dessert"]


def test_ajouter_recette_ecrit_base(client, monkeypatch):
    # Les cases Base cochées -> create_recipe reçoit la liste des bases.
    created = {}
    async def _create(nom, url="", repas="", tags=None, moment="", nature="Recette", base=None, base_servings=None):
        created["base"] = base
        created["nature"] = nature
        return {"id": "newid", "url": "https://notion/newid"}
    async def _noop(*a, **k):
        return {}
    monkeypatch.setattr(main.notion, "create_recipe", _create)
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)
    monkeypatch.setattr(main.notion, "append_instructions", _noop)
    monkeypatch.setattr(main.notion, "update_image", _noop)
    monkeypatch.setattr(main.notion, "update_portions", _noop)
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
    monkeypatch.setattr(main.notion, "update_portions", _noop)
    r = client.post("/recette/abc/enrichir", data={
        "repas": "Plat", "base": ["Poisson"], "ingredients_text": "1 pavé de saumon",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert meta["base"] == ["Poisson"]
    assert meta["nature"] == "Recette"


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
    monkeypatch.setattr(main.notion, "update_portions", _noop)
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
    monkeypatch.setattr(main.notion, "update_portions", _noop)
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
    monkeypatch.setattr(main.notion, "update_portions", _noop)
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
    monkeypatch.setattr(main.notion, "update_portions", _noop)
    monkeypatch.setattr(main.notion, "update_recipe_meta", _noop)
    monkeypatch.setattr(main.notion, "update_recipe_url", _url)
    r = client.post("/recette/abc/enrichir", data={
        "repas": "Plat", "source_url": "https://example.com/recette",
        "ingredients_text": "200 g riz",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert saved.get("url") == "https://example.com/recette"


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
    monkeypatch.setattr(main.notion, "update_portions", _noop)
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
    async def _create(nom, url="", repas="", tags=None, moment="", nature="Recette", base=None, base_servings=None):
        return {"id": "newid", "url": "https://notion/newid"}
    async def _noop(*a, **k):
        return {}
    async def _boom(*a, **k):
        raise RuntimeError("Notion KO")
    monkeypatch.setattr(main.notion, "create_recipe", _create)
    monkeypatch.setattr(main.notion, "update_ingredients", _boom)
    monkeypatch.setattr(main.notion, "append_instructions", _noop)
    monkeypatch.setattr(main.notion, "update_image", _noop)
    monkeypatch.setattr(main.notion, "update_portions", _noop)

    r = client.post("/ajouter-recette", data={
        "nom": "Gratin", "repas": "Plat",
        "ingredients_manual": "200 g pommes de terre\ncrème",
    })
    assert r.status_code == 200
    assert "créée dans Notion" in r.text
    assert "les ingrédients" in r.text
    # pas de succès trompeur
    assert "ajoutée avec succès" not in r.text


def test_ajouter_recette_ecrit_base_servings(client, monkeypatch):
    # Le champ Portions du formulaire → create_recipe + cache local.
    created = {}
    saved = {}
    async def _create(nom, url="", repas="", tags=None, moment="", nature="Recette", base=None, base_servings=None):
        created["base_servings"] = base_servings
        return {"id": "newid", "url": "https://notion/newid"}
    async def _save(*a, **k):
        saved["base_servings"] = k.get("base_servings")
        return None
    async def _noop(*a, **k):
        return {}
    monkeypatch.setattr(main.notion, "create_recipe", _create)
    monkeypatch.setattr(main.db, "save_enriched", _save)
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)
    monkeypatch.setattr(main.notion, "append_instructions", _noop)
    monkeypatch.setattr(main.notion, "update_image", _noop)
    r = client.post("/ajouter-recette", data={
        "nom": "Pâtes solo", "repas": "Plat", "base_servings": "1",
        "ingredients_manual": "70 g pâtes",
    })
    assert r.status_code == 200 and "succès" in r.text
    assert created["base_servings"] == 1
    assert saved["base_servings"] == 1


def test_enrichir_submit_ecrit_base_servings(client, monkeypatch):
    # Le champ Portions → update_portions Notion + cache local base_servings.
    async def _get(pid):
        return _recipe("Pâtes", id="abc", url="http://r")
    async def _enriched(nid):
        return None
    async def _instr(pid):
        return []
    portions = {}
    saved = {}
    async def _portions(page_id, n):
        portions["n"] = n
        return {}
    async def _save(*a, **k):
        saved["base_servings"] = k.get("base_servings")
        return None
    async def _noop(*a, **k):
        return {}
    monkeypatch.setattr(main.notion, "get_recipe", _get)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)
    monkeypatch.setattr(main.notion, "get_recipe_instructions", _instr)
    monkeypatch.setattr(main.notion, "update_portions", _portions)
    monkeypatch.setattr(main.db, "save_enriched", _save)
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)
    monkeypatch.setattr(main.notion, "rewrite_recipe_body", _noop)
    monkeypatch.setattr(main.notion, "update_image", _noop)
    monkeypatch.setattr(main.notion, "update_recipe_meta", _noop)
    r = client.post("/recette/abc/enrichir", data={
        "repas": "Plat", "base_servings": "1", "ingredients_text": "70 g pâtes",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert portions["n"] == 1
    assert saved["base_servings"] == 1
