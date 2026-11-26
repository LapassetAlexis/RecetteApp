"""Notion simulé en mémoire et lanceur de l'application pour les tests e2e.

Lancé en sous-processus par `conftest.py` :

    python -m tests.e2e.fake_notion <chemin_base> <port>

Le module remplace les méthodes du client Notion partagé (`app.deps.notion`)
par un catalogue de 10 recettes en mémoire (dont une au nom piégé
`Salade "César" <b>x</b>`), enregistre leurs ingrédients dans le cache local
(base SQLite temporaire) puis démarre uvicorn. Aucun appel réseau.
"""

import asyncio
import json
import os
import sys
import uuid
from datetime import date

NOM_PIEGE = 'Salade "César" <b>x</b>'

# « Aujourd'hui » vu par la duplication (« Refaire ») du serveur e2e : un
# mercredi fixe, pour que le test B3 (copie datée du jour, pas du lundi) ne
# dépende pas du jour réel où il tourne.
AUJOURDHUI_SERVEUR = date(2026, 12, 2)


def _recette(rid, nom, repas=("Plat",), base=(), nature="Recette", tags=(), note="", etat=""):
    return {
        "id": rid, "nom": nom, "url": "", "notion_url": "", "repas": list(repas),
        "tags": list(tags), "base": list(base), "nature": nature, "note": note,
        "etat": etat, "moment": "", "image": "",
    }


# Catalogue : 10 recettes. Les ingrédients sont choisis pour les parcours :
# « tomates » en grammes (bœuf) ET à la pièce (quiche) donnent deux lignes
# distinctes dans la liste de courses (B7).
RECETTES = [
    _recette("p1", "Poulet rôti", base=["Viande"], tags=["Rapide"]),
    _recette("c1", "Curry de lentilles", base=["Végé"]),
    _recette("s1", "Soupe de potiron", base=["Légume"]),
    _recette("g1", "Gratin dauphinois", base=["Féculent"]),
    _recette("q1", "Quiche lorraine", base=["Œuf"]),
    _recette("b1", "Bœuf bourguignon", base=["Viande"]),
    _recette("x1", NOM_PIEGE, base=["Viande"]),
    _recette("h1", "Haricots verts sautés", base=["Légume"]),
    _recette("r1", "Riz", repas=(), base=["Féculent"], nature="Ingrédient"),
    _recette("t1", "Tarte aux pommes", repas=["Dessert"]),
]

INGREDIENTS = {
    "p1": [{"nom": "poulet", "quantite": "1", "unite": "kg"},
           {"nom": "thym", "quantite": "2", "unite": "branches"}],
    "c1": [{"nom": "lentilles corail", "quantite": "250", "unite": "g"},
           {"nom": "lait de coco", "quantite": "40", "unite": "cl"}],
    "s1": [{"nom": "potiron", "quantite": "800", "unite": "g"},
           {"nom": "crème", "quantite": "20", "unite": "cl"}],
    "g1": [{"nom": "pommes de terre", "quantite": "1", "unite": "kg"},
           {"nom": "lait", "quantite": "50", "unite": "cl"}],
    "q1": [{"nom": "oeufs", "quantite": "3", "unite": ""},
           {"nom": "lardons", "quantite": "200", "unite": "g"},
           {"nom": "tomates", "quantite": "3", "unite": ""}],
    "b1": [{"nom": "boeuf", "quantite": "800", "unite": "g"},
           {"nom": "tomates", "quantite": "200", "unite": "g"}],
    "x1": [{"nom": "salade romaine", "quantite": "1", "unite": ""},
           {"nom": "parmesan", "quantite": "50", "unite": "g"}],
    "h1": [{"nom": "haricots verts", "quantite": "400", "unite": "g"}],
    "r1": [{"nom": "riz", "quantite": "300", "unite": "g"}],
    "t1": [{"nom": "pommes", "quantite": "6", "unite": ""}],
}


class FakeNotion:
    """Sous-ensemble du `NotionClient` utilisé par l'application."""

    def __init__(self):
        self.recettes = [dict(r) for r in RECETTES]

    def _get(self, page_id):
        return next((r for r in self.recettes if r["id"] == page_id), None)

    async def get_all_recipes(self, force=False):
        return [dict(r) for r in self.recettes]

    async def get_recipe(self, page_id):
        r = self._get(page_id)
        return dict(r) if r else None

    async def get_recipe_instructions(self, page_id):
        return ["Préparer les ingrédients.", "Cuire.", "Servir chaud."]

    async def create_recipe(self, nom="", repas=None, nature="Recette", base=None, **kw):
        rid = "new-" + uuid.uuid4().hex[:8]
        if isinstance(repas, str):
            repas = [repas] if repas else []
        if isinstance(base, str):
            base = [base] if base else []
        self.recettes.append(_recette(rid, nom, repas=repas or (), base=base or (), nature=nature or "Recette"))
        return {"id": rid, "url": ""}

    async def archive_recipe(self, page_id):
        self.recettes = [r for r in self.recettes if r["id"] != page_id]
        return {}

    async def _ok(self, *a, **k):
        return {}

    async def ensure_ingredients_field(self):
        return True

    def invalidate_cache(self):
        pass

    async def aclose(self):
        pass


_ECRITURES = ("update_ingredients", "update_image", "append_instructions", "rewrite_recipe_body",
              "update_recipe_url", "update_portions", "update_recipe_title", "update_recipe_meta",
              "update_rating", "update_status")


def install(client) -> FakeNotion:
    """Remplace les méthodes du client Notion partagé par le faux."""
    fake = FakeNotion()
    for name in ("get_all_recipes", "get_recipe", "get_recipe_instructions", "create_recipe",
                 "archive_recipe", "ensure_ingredients_field", "invalidate_cache", "aclose"):
        setattr(client, name, getattr(fake, name))
    for name in _ECRITURES:
        setattr(client, name, fake._ok)
    return fake


class _DateFigee(date):
    @classmethod
    def today(cls):
        return AUJOURDHUI_SERVEUR


def _figer_aujourdhui() -> None:
    """Fige `date.today()` du routeur planning (seul usage : la duplication)."""
    import app.routers.planning as routeur

    routeur.date = _DateFigee


def serve(db_path: str, port: int) -> None:
    os.environ["DATABASE_PATH"] = db_path
    for var in ("AUTH_USER", "AUTH_PASSWORD", "NOTION_TOKEN", "GEMINI_API_KEY", "GROQ_API_KEY"):
        os.environ.pop(var, None)

    import uvicorn

    import app.main as main

    main.db.path = db_path
    main.settings.database_path = db_path
    install(main.notion)
    _figer_aujourdhui()

    async def seed():
        await main.db.init()
        for r in RECETTES:
            if r["id"] in INGREDIENTS:
                await main.db.save_enriched(r["id"], r["nom"], ingredients=json.dumps(INGREDIENTS[r["id"]]))

    asyncio.run(seed())
    uvicorn.run(main.app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    serve(sys.argv[1], int(sys.argv[2]))
