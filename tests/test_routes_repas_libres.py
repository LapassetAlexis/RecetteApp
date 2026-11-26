"""Repas libres et briques créés depuis le planning, Notion/LLM simulés."""

import asyncio

import app.main as main
from tests.aides import _case, _construire, _recipe


def test_free_meal_creates_and_places(client, monkeypatch):
    """Un « repas libre » crée une recette minimale, la place dans le bon créneau
    et régénère une liste de courses incluant les ingrédients saisis."""

    poulet, soupe = _recipe("Poulet", id="p1"), _recipe("Soupe", id="s1")
    async def _all():
        return [poulet, soupe]

    created = {}
    async def _create(nom, url="", repas="", tags=None, etat="À essayer", moment="",
                      nature="Recette", base=None, base_servings=None):
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
