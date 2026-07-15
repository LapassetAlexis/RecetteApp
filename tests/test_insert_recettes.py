"""Tests du script d'insertion de recettes (scripts/insert_recettes), sans réseau."""

import asyncio
import json

import scripts.insert_recettes as ins
from app.notion_client import NotionClient


def test_charge_les_5_recettes():
    # Le JSON livré est chargeable et contient bien les 5 recettes « pour 1 ».
    recs = ins.load_recipes()
    assert len(recs) == 5
    assert recs[0]["nom"] == "Pâtes crémeuses poulet et épinards"
    assert all(r["base_servings"] == 1 for r in recs)
    # Chaque recette a des ingrédients et des instructions.
    assert all(r.get("ingredients") and r.get("instructions") for r in recs)


def test_structured_ingredients_parse():
    out = ins.structured_ingredients(["70 g pâtes sèches", "sel"])
    noms = {i["nom"] for i in out}
    assert "pâtes sèches" in noms and "sel" in noms
    pate = next(i for i in out if i["nom"] == "pâtes sèches")
    assert pate["quantite"] == "70" and pate["unite"] == "g"


def test_insert_apply_passe_base_servings_et_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(ins.settings, "notion_token", "x")

    calls = []
    existing = [{"nom": "Déjà là"}]

    async def _all(self, force=False):
        return existing

    async def _create(self, nom, url="", repas="", tags=None, moment="",
                      nature="Recette", base=None, base_servings=None):
        calls.append((nom, base_servings))
        return {"id": "id-" + nom}

    async def _noop(self, *a, **k):
        return {}

    monkeypatch.setattr(NotionClient, "get_all_recipes", _all)
    monkeypatch.setattr(NotionClient, "create_recipe", _create)
    monkeypatch.setattr(NotionClient, "update_ingredients", _noop)
    monkeypatch.setattr(NotionClient, "rewrite_recipe_body", _noop)
    monkeypatch.setattr(NotionClient, "update_image", _noop)

    from app.main import db as app_db
    saved = []

    async def _save(*a, **k):
        saved.append(k.get("base_servings"))

    monkeypatch.setattr(app_db, "save_enriched", _save)

    data = [
        {"nom": "Déjà là", "base_servings": 2, "ingredients": ["100 g riz"]},
        {"nom": "Nouvelle", "base_servings": 1, "ingredients": ["70 g pâtes"],
         "instructions": "Cuire."},
    ]
    p = tmp_path / "r.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    counters = asyncio.run(ins.run(apply=True, path=str(p)))
    assert counters["created"] == 1 and counters["skipped"] == 1
    # create_recipe n'est appelé QUE pour la recette absente, avec sa base.
    assert calls == [("Nouvelle", 1)]
    assert saved == [1]


def test_insert_dry_run_ne_cree_rien(monkeypatch, tmp_path):
    monkeypatch.setattr(ins.settings, "notion_token", "x")

    async def _all(self, force=False):
        return []

    created = []

    async def _create(self, *a, **k):
        created.append(k.get("nom"))
        return {"id": "x"}

    monkeypatch.setattr(NotionClient, "get_all_recipes", _all)
    monkeypatch.setattr(NotionClient, "create_recipe", _create)

    data = [{"nom": "Nouvelle", "base_servings": 1, "ingredients": ["70 g pâtes"]}]
    p = tmp_path / "r.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    counters = asyncio.run(ins.run(apply=False, path=str(p)))
    assert counters["created"] == 0
    assert created == []  # dry-run : aucune écriture
