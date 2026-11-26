"""Fabriques partagées par les tests de routes (recettes simulées, cases du
constructeur, planning construit). La fixture `client` est dans `conftest.py`."""

import json

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
