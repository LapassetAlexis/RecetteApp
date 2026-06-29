"""Tests de la couche SQLite (sans réseau)."""

import asyncio
import json

from app.database import Database


def _db(tmp_path):
    db = Database()
    db.path = str(tmp_path / "test.db")
    asyncio.run(db.init())
    return db


def _plats():
    return [
        {"notion_id": "n1", "recipe_name": "Poulet", "repas_type": "Plat", "jour": 1, "moment": "midi"},
        {"notion_id": "n2", "recipe_name": "Soupe", "repas_type": "Plat", "jour": 1, "moment": "soir"},
    ]


def test_save_and_get_planning(tmp_path):
    db = _db(tmp_path)
    data = json.dumps({"plats": [], "liste_courses": []})
    pid = asyncio.run(db.save_planning("2026-01-05", "Hiver", 4, "", data, _plats()))
    p = asyncio.run(db.get_planning_with_recipes(pid))
    assert p["week_start"] == "2026-01-05"
    assert len(p["recipes"]) == 2
    assert {r["recipe_name"] for r in p["recipes"]} == {"Poulet", "Soupe"}


def test_update_planning_does_not_duplicate(tmp_path):
    # Régression du bug : update_meal créait un nouveau planning au lieu d'éditer.
    db = _db(tmp_path)
    pid = asyncio.run(db.save_planning("2026-01-05", "Hiver", 4, "", "{}", _plats()))
    asyncio.run(db.mark_planning_valid(pid))
    new_plats = [
        {"notion_id": "n3", "recipe_name": "Curry", "repas_type": "Plat", "jour": 1, "moment": "midi"},
        {"notion_id": "n2", "recipe_name": "Soupe", "repas_type": "Plat", "jour": 1, "moment": "soir"},
    ]
    asyncio.run(db.update_planning(pid, json.dumps({"liste_courses": [1]}), new_plats))
    # un seul planning en base
    assert len(asyncio.run(db.list_plannings(limit=10))) == 1
    p = asyncio.run(db.get_planning_with_recipes(pid))
    assert {r["recipe_name"] for r in p["recipes"]} == {"Curry", "Soupe"}
    assert json.loads(p["data_json"])["liste_courses"] == [1]


def test_recent_recipe_names(tmp_path):
    db = _db(tmp_path)
    pid = asyncio.run(db.save_planning("2026-01-05", "Hiver", 4, "", "{}", _plats()))
    asyncio.run(db.mark_planning_valid(pid))
    recent = asyncio.run(db.get_recent_recipe_names(weeks=4))
    assert "Poulet" in recent and "Soupe" in recent


def test_drafts_excluded_until_valid(tmp_path):
    # Un brouillon (non validé) n'apparaît pas dans l'historique ni dans
    # l'exclusion des recettes récentes ; après validation, si.
    db = _db(tmp_path)
    pid = asyncio.run(db.save_planning("2026-01-05", "Hiver", 4, "", "{}", _plats()))
    assert asyncio.run(db.list_plannings()) == []
    assert asyncio.run(db.get_recent_recipe_names(weeks=4)) == set()
    asyncio.run(db.mark_planning_valid(pid))
    assert len(asyncio.run(db.list_plannings())) == 1
    assert "Poulet" in asyncio.run(db.get_recent_recipe_names(weeks=4))


def test_delete_draft_plannings(tmp_path):
    db = _db(tmp_path)
    p1 = asyncio.run(db.save_planning("2026-01-05", "Hiver", 4, "", "{}", _plats()))
    asyncio.run(db.mark_planning_valid(p1))
    asyncio.run(db.save_planning("2026-01-12", "Hiver", 4, "", "{}", _plats()))  # brouillon
    asyncio.run(db.delete_draft_plannings())
    assert len(asyncio.run(db.list_plannings())) == 1  # seul le validé reste


def test_enriched_cache_roundtrip(tmp_path):
    db = _db(tmp_path)
    ings = json.dumps([{"nom": "farine", "quantite": "200", "unite": "g"}])
    asyncio.run(db.save_enriched("n1", "Crepes", ingredients=ings))
    got = asyncio.run(db.get_enriched("n1"))
    assert got["recipe_name"] == "Crepes"
    assert json.loads(got["ingredients"])[0]["nom"] == "farine"
    assert asyncio.run(db.get_enriched("inconnu")) is None


def test_get_last_planning_empty(tmp_path):
    db = _db(tmp_path)
    assert asyncio.run(db.get_last_planning()) is None
