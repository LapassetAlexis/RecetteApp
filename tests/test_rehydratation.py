"""Réhydratation du cache d'ingrédients depuis la copie texte Notion."""

import asyncio
import json

import aiosqlite

import app.main as main
from app.notion_client import NotionClient
from app.services import backup
from app.services.shopping import _ingredients_to_text
from tests import test_routes
from tests.test_routes import _recipe

client = test_routes.client  # fixture réutilisée


def _page(props):
    return {"id": "pid", "url": "https://notion.so/pid", "properties": props}


def test_parse_page_lit_la_propriete_ingredients():
    n = NotionClient()
    r = n._parse_page(_page({
        "Nom": {"title": [{"plain_text": "Tarte"}]},
        "Ingrédients": {"rich_text": [{"plain_text": "- 200 g de farine\n"}, {"plain_text": "- 3 œufs"}]},
    }))
    assert r["ingredients_text"] == "- 200 g de farine\n- 3 œufs"
    # Colonne absente ou vide : chaîne vide, sans erreur.
    assert n._parse_page(_page({}))["ingredients_text"] == ""
    assert n._parse_page(_page({"Ingrédients": {"rich_text": None}}))["ingredients_text"] == ""


def test_ingredients_from_text_relit_le_format_ecrit():
    ings = [{"nom": "épinards", "quantite": "700", "unite": "g"},
            {"nom": "œufs", "quantite": "4", "unite": ""},
            {"nom": "huile", "quantite": "", "unite": "c. à s."}]
    assert backup.ingredients_from_text(_ingredients_to_text(ings)) == ings
    assert backup.ingredients_from_text("") == []
    assert backup.ingredients_from_text("\n  \n- \n") == []


def _notion(recettes):
    class _N:
        async def get_all_recipes(self, force=False):
            assert force  # relecture fraîche
            return recettes
    return _N()


def test_rehydrate_apres_suppression_du_cache(client, monkeypatch):
    recettes = [
        {**_recipe("Tarte", id="t1"), "base_servings": 6,
         "ingredients_text": "- 200 g de farine\n- 3 œufs"},
        {**_recipe("Curry", id="c1"), "base_servings": 4, "ingredients_text": "- 1 l de lait de coco"},
        {**_recipe("Salade", id="s1"), "ingredients_text": ""},
        {**_recipe("Vide", id="v1"), "ingredients_text": "- 1 kg de pommes"},
        {**_recipe("Sans id", id=""), "ingredients_text": "- 1 kg de pommes"},
    ]

    async def _all(force=False):
        return recettes
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)

    # Le curry a des ingrédients saisis localement (jamais écrasés) ; « Vide »
    # a une entrée sans ingrédient mais avec une durée (gardée).
    asyncio.run(main.db.save_enriched("c1", "Curry", ingredients=json.dumps([{"nom": "maison"}])))
    asyncio.run(main.db.save_enriched("v1", "Vide", ingredients="[]", cuisson_minutes=25, base_servings=2))
    asyncio.run(main.db.save_enriched("t1", "Tarte", ingredients=json.dumps([{"nom": "x"}])))

    async def _purge_tarte():  # suppression d'enriched_recipes pour la tarte
        async with aiosqlite.connect(main.db.path) as conn:
            await conn.execute("DELETE FROM enriched_recipes WHERE notion_id = 't1'")
            await conn.commit()
    asyncio.run(_purge_tarte())

    r = client.post("/admin/rehydrater")
    assert r.status_code == 200
    assert "2 recette(s) restaurée(s)" in r.text and "1 déjà en cache" in r.text

    tarte = asyncio.run(main.db.get_enriched("t1"))
    assert json.loads(tarte["ingredients"]) == [
        {"nom": "farine", "quantite": "200", "unite": "g"},
        {"nom": "œufs", "quantite": "3", "unite": ""},
    ]
    assert tarte["base_servings"] == 6
    assert json.loads(asyncio.run(main.db.get_enriched("c1"))["ingredients"]) == [{"nom": "maison"}]
    vide = asyncio.run(main.db.get_enriched("v1"))
    assert json.loads(vide["ingredients"]) == [{"nom": "pommes", "quantite": "1", "unite": "kg"}]
    assert vide["cuisson_minutes"] == 25 and vide["base_servings"] == 2
    assert asyncio.run(main.db.get_enriched("s1")) is None

    # Deuxième passage : tout est déjà en cache, rien ne bouge.
    stats = asyncio.run(backup.rehydrate_from_notion(main.db, _notion(recettes)))
    assert stats["restaurees"] == 0 and stats["deja_en_cache"] == 3 and stats["sans_texte"] == 1


def test_rehydrate_garde_un_cache_illisible(tmp_path):
    from app.database import Database
    db = Database()
    db.path = str(tmp_path / "r.db")
    asyncio.run(db.init())
    asyncio.run(db.save_enriched("a", "A", ingredients="{pas du json"))
    stats = asyncio.run(backup.rehydrate_from_notion(
        db, _notion([{"id": "a", "nom": "A", "ingredients_text": "- 1 kg de pommes"}])))
    assert stats["deja_en_cache"] == 1
    assert asyncio.run(db.get_enriched("a"))["ingredients"] == "{pas du json"


def test_rehydrate_notion_injoignable(client, monkeypatch):
    async def _boom(force=False):
        raise RuntimeError("réseau")
    monkeypatch.setattr(main.notion, "get_all_recipes", _boom)
    r = client.post("/admin/rehydrater")
    assert r.status_code == 502 and "injoignable" in r.text
