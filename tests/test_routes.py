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


def _case(jour, moment, main=None, persons=4, group=None, accs=None):
    """Construit une case du champ `meals` du constructeur.

    main / accs = recettes (dicts type _recipe) → convertis en items
    {notion_id, nom, nature}."""
    c = {"jour": jour, "moment": moment, "persons": persons}
    if moment == "midi":
        c["group"] = group if group is not None else jour
    if main:
        c["main"] = {"notion_id": main["id"], "nom": main["nom"],
                     "nature": main.get("nature", "Recette")}
    c["accompagnements"] = [{"notion_id": a["id"], "nom": a["nom"]} for a in (accs or [])]
    return c


def _construire(client, cases, week_start="2026-01-05"):
    """POST /construire avec une liste de cases ; renvoie l'id du planning créé."""
    loc = client.post("/construire", data={
        "week_start": week_start, "meals": json.dumps(cases),
    }, follow_redirects=False).headers["location"]
    return int(loc.rsplit("/", 1)[1])


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


def test_construire_success_then_view(client, monkeypatch):
    poulet, soupe = _recipe("Poulet", id="p1"), _recipe("Soupe", id="s1")
    async def _all():
        return [poulet, soupe]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    cases = [_case(1, "midi", poulet, persons=2), _case(1, "soir", soupe, persons=2)]
    r = client.post("/construire", data={"week_start": "2026-01-05",
                                         "meals": json.dumps(cases)},
                    follow_redirects=False)
    assert r.status_code == 303
    assert client.get(r.headers["location"]).status_code == 200


def test_construire_sans_repas(client, monkeypatch):
    """Aucun repas choisi → on re-affiche le formulaire avec un message."""
    async def _all():
        return [_recipe("Poulet", id="p1")]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    # une case avec convives mais sans plat principal → ignorée → aucun plat
    cases = [_case(1, "midi", None, persons=2)]
    r = client.post("/construire", data={"week_start": "2026-01-05",
                                         "meals": json.dumps(cases)})
    assert r.status_code == 200
    assert "Aucun repas" in r.text


def test_construire_error_preserves_week(client, monkeypatch):
    async def _boom():
        raise RuntimeError("Notion down")
    monkeypatch.setattr(main.notion, "get_all_recipes", _boom)
    cases = [_case(1, "midi", _recipe("X", id="x"), persons=2)]
    r = client.post("/construire", data={"week_start": "2026-02-09",
                                         "meals": json.dumps(cases)})
    assert r.status_code == 200
    assert "2026-02-09" in r.text        # la semaine saisie est repassée au formulaire


def test_api_catalogue(client, monkeypatch):
    async def _all():
        return [_recipe("Poulet", id="p1", base=["Viande"]),
                _recipe("Riz", id="r1", repas="", base=["Féculent"], nature="Ingrédient")]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    data = client.get("/api/catalogue").json()
    assert isinstance(data, list) and len(data) == 2
    poulet = next(x for x in data if x["nom"] == "Poulet")
    assert poulet["id"] == "p1" and poulet["nature"] == "Recette"
    assert poulet["base"] == ["Viande"]
    riz = next(x for x in data if x["nom"] == "Riz")
    assert riz["nature"] == "Ingrédient" and riz["base"] == ["Féculent"]


def test_construire_complet(client, monkeypatch):
    """Constructeur de bout en bout : une recette, une brique + accompagnements,
    une case absente et deux midis groupés partageant le repas. Vérifie les
    plats créés, l'application des groupes, les personnes par repas, la liste de
    courses (ingrédients ancrés au cache) et la redirection /planning."""
    import asyncio

    poulet = _recipe("Poulet", id="p1", base=["Viande"])
    steak = _recipe("Steak", id="st", repas="", base=["Viande"], nature="Ingrédient")
    riz = _recipe("Riz", id="rz", repas="", base=["Féculent"], nature="Ingrédient")
    brocoli = _recipe("Brocoli", id="br", repas="", base=["Légume"], nature="Ingrédient")

    async def _all():
        return [poulet, steak, riz, brocoli]
    async def _enriched(nid):
        return {"ingredients": json.dumps([{"nom": nid, "quantite": "100", "unite": "g"}])}
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)

    cases = [
        # lun + mar midi : même groupe 1, seul lun porte le repas (Poulet, 2 pers)
        _case(1, "midi", poulet, persons=2, group=1),
        _case(2, "midi", None, persons=2, group=1),
        # lun soir : brique Steak + accompagnements Riz & Brocoli, 4 pers
        _case(1, "soir", steak, persons=4, accs=[riz, brocoli]),
        # mar soir : absent (0 convive) → ignoré
        _case(2, "soir", poulet, persons=0),
    ]
    pid = _construire(client, cases)

    plats = json.loads(asyncio.run(main.db.get_planning_with_recipes(pid))["data_json"])["plats"]
    by_key = {(p["jour"], p["moment"]): p for p in plats}

    # Case absente non générée ; 3 repas produits (2 midis groupés + 1 soir).
    assert (2, "soir") not in by_key
    assert set(by_key) == {(1, "midi"), (2, "midi"), (1, "soir")}

    # Groupe appliqué : les 2 midis partagent le repas du 1er rempli (Poulet).
    assert by_key[(1, "midi")]["nom_recette"] == "Poulet"
    assert by_key[(2, "midi")]["nom_recette"] == "Poulet"

    # Personnes PAR REPAS respectées.
    assert by_key[(1, "midi")]["persons"] == 2
    assert by_key[(2, "midi")]["persons"] == 2
    assert by_key[(1, "soir")]["persons"] == 4

    # La brique + ses accompagnements sont posés au soir.
    soir = by_key[(1, "soir")]
    assert soir["nature"] == "Ingrédient" and soir["nom_recette"] == "Steak"
    assert {a["nom_recette"] for a in soir["accompagnements"]} == {"Riz", "Brocoli"}

    # Liste de courses générée, mise à l'échelle PAR REPAS.
    shop = {i["nom"]: i for i in json.loads(
        asyncio.run(main.db.get_planning_with_recipes(pid))["data_json"])["liste_courses"]}
    # p1 : 2 midis (2 pers) × 100 g / 4 = 50 g chacun → fusionnés = 100 g.
    assert shop["p1"]["quantite"] == "100"
    # brique + accompagnements soir (4 pers) × 100 g / 4 = 100 g.
    assert shop["st"]["quantite"] == "100"
    assert shop["rz"]["quantite"] == "100"
    assert shop["br"]["quantite"] == "100"


def test_update_meal_updates_without_duplicate(client, monkeypatch):
    poulet, soupe = _recipe("Poulet", id="p1"), _recipe("Soupe", id="s1")
    async def _all():
        return [poulet, soupe, _recipe("Curry", id="c1")]
    async def _ext(nom, url="", nb=4):
        return {"ingredients": [{"nom": "sel", "quantite": "1", "unite": "pincée"}]}
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.llm, "extract_ingredients", _ext)

    pid = _construire(client, [_case(1, "midi", poulet, persons=2),
                               _case(1, "soir", soupe, persons=2)])

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
    poulet, soupe = _recipe("Poulet", id="p1"), _recipe("Soupe", id="s1")
    async def _all():
        return [poulet, soupe]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    pid = _construire(client, [_case(1, "midi", poulet, persons=2),
                               _case(1, "soir", soupe, persons=2)])
    # brouillon : listé dans la section Brouillons, PAS dans les validées ;
    # bandeau de validation présent sur la page planning.
    hist = client.get("/historique").text
    assert "📝 Brouillons" in hist
    assert "Semaines validées" not in hist
    assert "Valider la semaine" in client.get(f"/planning/{pid}").text
    # validation → rejoint les semaines validées
    assert client.post(f"/planning/{pid}/valider").json().get("success")
    assert "Semaines validées" in client.get("/historique").text
    assert "Semaine validée" in client.get(f"/planning/{pid}").text


def _enregistrer_brouillon(client, cases, week_start="2026-01-05", draft_id=""):
    """POST /construire avec action=brouillon ; renvoie la réponse (sans suivre
    la redirection) pour inspecter le Location."""
    return client.post("/construire", data={
        "week_start": week_start, "meals": json.dumps(cases),
        "action": "brouillon", "draft_id": draft_id,
    }, follow_redirects=False)


def test_enregistrer_brouillon_reste_en_brouillon(client, monkeypatch):
    """action=brouillon → planning valide=0 avec data["builder"], redirection
    vers /?draft=<id>&saved=1, et PAS de validation."""
    import asyncio
    poulet = _recipe("Poulet", id="p1")
    async def _all():
        return [poulet]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    cases = [_case(1, "midi", poulet, persons=2)]
    r = _enregistrer_brouillon(client, cases)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/?draft=") and "saved=1" in loc
    pid = int(loc.split("draft=")[1].split("&")[0])
    # brouillon (valide=0) : pas dans les validées, présent dans list_drafts
    assert asyncio.run(main.db.list_plannings()) == []
    drafts = asyncio.run(main.db.list_drafts())
    assert [d["id"] for d in drafts] == [pid]
    # data["builder"] contient la grille brute soumise
    data = json.loads(asyncio.run(main.db.get_planning_with_recipes(pid))["data_json"])
    assert data["builder"] == cases


def test_reouvrir_brouillon_injecte_builder(client, monkeypatch):
    """GET /?draft=<id> hydrate la page : INITIAL_BUILDER non vide + draft_id."""
    poulet = _recipe("Poulet", id="p1")
    async def _all():
        return [poulet]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    cases = [_case(1, "midi", poulet, persons=2)]
    r = _enregistrer_brouillon(client, cases)
    pid = int(r.headers["location"].split("draft=")[1].split("&")[0])
    html = client.get(f"/?draft={pid}").text
    assert "Poulet" in html                       # le nom du plat est dans le builder
    assert f'value="{pid}"' in html               # draft_id pré-rempli
    assert "INITIAL_BUILDER = []" not in html      # builder hydraté (non vide)
    # brouillon inconnu / validé : grille vierge
    html2 = client.get("/?draft=99999").text
    assert "INITIAL_BUILDER = []" in html2


def test_maj_brouillon_ne_cree_pas_de_planning(client, monkeypatch):
    """draft_id fourni → met à jour le brouillon existant, aucun nouveau créé."""
    import asyncio
    poulet, soupe = _recipe("Poulet", id="p1"), _recipe("Soupe", id="s1")
    async def _all():
        return [poulet, soupe]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    r = _enregistrer_brouillon(client, [_case(1, "midi", poulet, persons=2)])
    pid = int(r.headers["location"].split("draft=")[1].split("&")[0])
    # ré-enregistrement avec draft_id : même planning mis à jour
    cases2 = [_case(1, "midi", soupe, persons=3)]
    r2 = _enregistrer_brouillon(client, cases2, draft_id=str(pid))
    pid2 = int(r2.headers["location"].split("draft=")[1].split("&")[0])
    assert pid2 == pid
    drafts = asyncio.run(main.db.list_drafts())
    assert len(drafts) == 1                        # pas de nouveau planning
    p = asyncio.run(main.db.get_planning_with_recipes(pid))
    assert {rec["recipe_name"] for rec in p["recipes"]} == {"Soupe"}


def test_brouillons_multiples_coexistent(client, monkeypatch):
    """Deux brouillons enregistrés coexistent (pas de purge en construire)."""
    import asyncio
    poulet = _recipe("Poulet", id="p1")
    async def _all():
        return [poulet]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    _enregistrer_brouillon(client, [_case(1, "midi", poulet, persons=2)])
    _enregistrer_brouillon(client, [_case(2, "midi", poulet, persons=2)])
    assert len(asyncio.run(main.db.list_drafts())) == 2


def test_historique_affiche_brouillons_et_suppression(client, monkeypatch):
    """L'historique liste la section Brouillons ; la suppression retire le
    brouillon via /planning/{id}/supprimer."""
    import asyncio
    poulet = _recipe("Poulet", id="p1")
    async def _all():
        return [poulet]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    r = _enregistrer_brouillon(client, [_case(1, "midi", poulet, persons=2)])
    pid = int(r.headers["location"].split("draft=")[1].split("&")[0])
    hist = client.get("/historique").text
    assert "📝 Brouillons" in hist
    assert f"/?draft={pid}" in hist                # lien Reprendre
    # suppression
    assert client.post(f"/planning/{pid}/supprimer").json().get("success")
    assert asyncio.run(main.db.list_drafts()) == []
    assert client.post(f"/planning/{pid}/supprimer").json().get("error")


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
    poulet, soupe = _recipe("Poulet", id="p1"), _recipe("Soupe", id="s1")
    async def _all():
        return [poulet, soupe, _recipe("Curry", id="c1")]
    # Liste de courses ANCRÉE sur le cache DB (plus de batch LLM à l'aveugle).
    async def _enriched(nid):
        return {"ingredients": json.dumps([{"nom": "riz", "quantite": "100", "unite": "g"}])}
    async def _noop(*a, **k):
        return {}
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)

    # 2 repas lundi à 8 personnes chacun.
    pid = _construire(client, [_case(1, "midi", poulet, persons=8),
                               _case(1, "soir", soupe, persons=8)])

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
    poulet, soupe = _recipe("Poulet", id="p1"), _recipe("Soupe", id="s1")
    async def _all():
        return [poulet, soupe]
    async def _enriched(nid):
        return {"ingredients": json.dumps([{"nom": "riz", "quantite": "100", "unite": "g"}])}
    async def _noop(*a, **k):
        return {}
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)
    pid = _construire(client, [_case(1, "midi", poulet, persons=2),
                               _case(1, "soir", soupe, persons=2)])
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


# ── Accompagnements MULTIPLES ──────────────────────────────────────

def _side_planning(client, monkeypatch):
    """Planning à un repas (Poulet, midi jour 1) + catalogue de 2 légumes
    (Haricots, Carottes), chacun avec ses ingrédients en cache."""
    poulet = _recipe("Poulet", id="p1")
    async def _all():
        return [poulet,
                _recipe("Haricots", id="h1", base=["Légume"]),
                _recipe("Carottes", id="c1", base=["Légume"])]
    _INGS = {
        "p1": [{"nom": "poulet", "quantite": "200", "unite": "g"}],
        "h1": [{"nom": "haricots", "quantite": "150", "unite": "g"}],
        "c1": [{"nom": "carottes", "quantite": "100", "unite": "g"}],
    }
    async def _enriched(nid):
        return {"ingredients": json.dumps(_INGS[nid])} if nid in _INGS else None
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)
    return _construire(client, [_case(1, "midi", poulet, persons=4)])


def test_add_side_appends_multiple(client, monkeypatch):
    """add-side ajoute (APPEND) : deux accompagnements coexistent, doublon ignoré,
    et leurs ingrédients figurent tous dans la liste de courses."""
    pid = _side_planning(client, monkeypatch)
    assert client.post(f"/api/add-side/{pid}",
                       json={"jour": 1, "moment": "midi", "nom": "Haricots"}).json()["success"]
    d = client.post(f"/api/add-side/{pid}",
                    json={"jour": 1, "moment": "midi", "nom": "Carottes"}).json()
    assert d["success"]
    midi = next(p for p in d["plats"] if p["jour"] == 1 and p["moment"] == "midi")
    assert [a["nom_recette"] for a in midi["accompagnements"]] == ["Haricots", "Carottes"]

    # Doublon (même nom) : ignoré, toujours 2 accompagnements.
    d3 = client.post(f"/api/add-side/{pid}",
                     json={"jour": 1, "moment": "midi", "nom": "Haricots"}).json()
    midi = next(p for p in d3["plats"] if p["jour"] == 1 and p["moment"] == "midi")
    assert len(midi["accompagnements"]) == 2

    # Les ingrédients des DEUX accompagnements sont dans les courses.
    noms = {i["nom"].lower() for i in d3["liste_courses"]}
    assert {"haricots", "carottes"} <= noms


def test_remove_side_removes_the_right_one(client, monkeypatch):
    """remove-side retire l'accompagnement ciblé (par nom), garde les autres,
    et met à jour la liste de courses en conséquence."""
    pid = _side_planning(client, monkeypatch)
    client.post(f"/api/add-side/{pid}", json={"jour": 1, "moment": "midi", "nom": "Haricots"})
    client.post(f"/api/add-side/{pid}", json={"jour": 1, "moment": "midi", "nom": "Carottes"})
    d = client.post(f"/api/remove-side/{pid}",
                    json={"jour": 1, "moment": "midi", "nom": "Haricots"}).json()
    assert d["success"]
    midi = next(p for p in d["plats"] if p["jour"] == 1 and p["moment"] == "midi")
    assert [a["nom_recette"] for a in midi["accompagnements"]] == ["Carottes"]
    noms = {i["nom"].lower() for i in d["liste_courses"]}
    assert "carottes" in noms and "haricots" not in noms


def test_update_meal_clears_sides(client, monkeypatch):
    """Remplacer le plat principal réinitialise ses accompagnements."""
    pid = _side_planning(client, monkeypatch)
    client.post(f"/api/add-side/{pid}", json={"jour": 1, "moment": "midi", "nom": "Haricots"})
    d = client.post(f"/api/update-meal/{pid}",
                    json={"jour": 1, "moment": "midi", "nouvelle_recette": "Carottes"}).json()
    assert d["success"]
    midi = next(p for p in d["plats"] if p["jour"] == 1 and p["moment"] == "midi")
    assert midi["nom_recette"] == "Carottes"
    assert midi["accompagnements"] == []


def test_legacy_accompagnement_read_as_list(client, monkeypatch):
    """Ancien planning stockant `accompagnement` (dict) : lu comme liste à 1
    (affichage + helper) et inclus dans les courses via plat_accompagnements."""
    import asyncio
    legacy = {"jour": 1, "moment": "midi", "nom_recette": "Poulet", "notion_id": "p1",
              "accompagnement": {"nom_recette": "Haricots", "notion_id": "h1",
                                 "url": "", "notion_url": ""}}
    # Helper tolérant : le dict singulier devient une liste à 1.
    assert len(main.plat_accompagnements(legacy)) == 1
    assert main.plat_accompagnements(legacy)[0]["nom_recette"] == "Haricots"
    # accompagnement absent / None -> liste vide.
    assert main.plat_accompagnements({"nom_recette": "X"}) == []
    assert main.plat_accompagnements({"accompagnement": None}) == []

    data = {"plats": [legacy], "liste_courses": [], "per_day": "4,4,4,4,4,4,4"}
    pid = asyncio.run(main.db.save_planning(
        week_start="2026-01-05", saison="Hiver", nb_personnes=4, ingredients_force="",
        data_json=json.dumps(data, ensure_ascii=False),
        recipes=[{"notion_id": "p1", "recipe_name": "Poulet", "repas_type": "Plat",
                  "jour": 1, "moment": "midi"}],
    ))
    # L'accompagnement legacy est bien rendu dans la page.
    assert "Haricots" in client.get(f"/planning/{pid}").text


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
    # Crée un planning, le duplique -> nouveau brouillon avec plats copiés
    poulet = _recipe("Poulet", id="p1")
    async def _all():
        return [poulet]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    pid = _construire(client, [_case(1, "midi", poulet, persons=2)])
    d = client.post(f"/planning/{pid}/dupliquer").json()
    assert d["success"] and d["planning_id"] != pid
    # la copie est un brouillon (bandeau de validation présent)
    assert "Valider la semaine" in client.get(f"/planning/{d['planning_id']}").text


def test_planning_warns_non_enrichi(client, monkeypatch):
    import json
    poulet = _recipe("Poulet", id="n1")
    async def _all():
        return [poulet]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    pid = _construire(client, [_case(1, "midi", poulet, persons=2)])
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


def test_construire_case_absente(client, monkeypatch):
    """Une case à 0 convive (absent) n'est pas générée et le créneau apparaît
    comme désactivé dans la page planning."""
    poulet = _recipe("Poulet", id="p1")
    async def _all():
        return [poulet]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    # midi 1 présent, midi 1 soir absent
    pid = _construire(client, [_case(1, "midi", poulet, persons=2),
                               _case(1, "soir", poulet, persons=0)])
    import asyncio
    plats = json.loads(asyncio.run(main.db.get_planning_with_recipes(pid))["data_json"])["plats"]
    assert {(p["jour"], p["moment"]) for p in plats} == {(1, "midi")}
    page = client.get(f"/planning/{pid}").text
    assert "Absent" in page  # placeholder rendu pour le créneau off


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


def test_free_meal_creates_and_places(client, monkeypatch):
    """Un « repas libre » crée une recette minimale, la place dans le bon créneau
    et régénère une liste de courses incluant les ingrédients saisis."""
    import asyncio

    poulet, soupe = _recipe("Poulet", id="p1"), _recipe("Soupe", id="s1")
    async def _all():
        return [poulet, soupe]

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
    monkeypatch.setattr(main.notion, "create_recipe", _create)
    monkeypatch.setattr(main.notion, "update_ingredients", _upd_ings)
    # db.save_enriched / get_enriched : vrai cache (round-trip sur la db temporaire)

    pid = _construire(client, [_case(1, "midi", poulet, persons=2),
                               _case(1, "soir", soupe, persons=2)])

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
    poulet = _recipe("Poulet", id="p1")
    async def _all():
        return [poulet]
    appels = {"create": 0}
    async def _create(**kw):
        appels["create"] += 1
        return {"id": "x"}
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.notion, "create_recipe", _create)

    pid = _construire(client, [_case(1, "midi", poulet, persons=2)])
    # jour 1 soir n'existe pas dans ce planning
    resp = client.post(f"/api/free-meal/{pid}", json={
        "jour": 1, "moment": "soir", "nom": "Test", "ingredients": ""})
    assert resp.json().get("error")
    assert appels["create"] == 0  # aucune page Notion orpheline


def _brique_mocks(monkeypatch):
    """Prépare les mocks partagés des tests brique : catalogue Notion minimal,
    capture de create_recipe/update_ingredients. Renvoie
    (created, ings_ecrits, poulet, soupe) pour inspection. Le cache DB reste RÉEL."""
    poulet, soupe = _recipe("Poulet", id="p1"), _recipe("Soupe", id="s1")
    async def _all():
        return [poulet, soupe]
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
    monkeypatch.setattr(main.notion, "create_recipe", _create)
    monkeypatch.setattr(main.notion, "update_ingredients", _upd_ings)
    return created, ings_ecrits, poulet, soupe


def test_brique_plat_creates_and_places(client, monkeypatch):
    """Quick-create brique en slot plat : crée une recette Nature=Ingrédient +
    Base, avec pour ingrédient elle-même, la place et l'inclut dans les courses."""
    import asyncio
    created, ings_ecrits, poulet, soupe = _brique_mocks(monkeypatch)

    pid = _construire(client, [_case(1, "midi", poulet, persons=2),
                               _case(1, "soir", soupe, persons=2)])

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
    created, ings_ecrits, poulet, soupe = _brique_mocks(monkeypatch)

    pid = _construire(client, [_case(1, "midi", poulet, persons=2),
                               _case(1, "soir", soupe, persons=2)])

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
    accs = midi["accompagnements"]
    assert len(accs) == 1
    acc = accs[0]
    assert acc["nom_recette"] == "Brocolis"
    assert acc["notion_id"] == "brique1"
    assert acc["base"] == ["Légume"]

    # Ses ingrédients figurent dans la liste de courses.
    noms = {i["nom"].lower() for i in data["liste_courses"]}
    assert "brocolis" in noms


def test_brique_creneau_introuvable(client, monkeypatch):
    """Créneau inexistant → erreur, aucune recette brique créée."""
    poulet = _recipe("Poulet", id="p1")
    async def _all():
        return [poulet]
    appels = {"create": 0}
    async def _create(**kw):
        appels["create"] += 1
        return {"id": "x"}
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.notion, "create_recipe", _create)

    pid = _construire(client, [_case(1, "midi", poulet, persons=2)])
    # jour 1 soir n'existe pas dans ce planning
    resp = client.post(f"/api/brique/{pid}", json={
        "jour": 1, "moment": "soir", "slot": "plat", "nom": "Riz", "base": ["Féculent"]})
    assert resp.json().get("error")
    assert appels["create"] == 0  # pas de page Notion orpheline
