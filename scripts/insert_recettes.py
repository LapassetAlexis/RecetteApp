"""Insertion en masse de recettes dans Notion depuis un fichier JSON.

Lit une liste de recettes (`scripts/recettes_a_inserer.json` par défaut) et
insère chacune : page Notion (create_recipe avec base de portions), ingrédients
(propriété + cache local structuré), instructions (corps de page) et image.

Format JSON par recette :
    {
      "nom": "…",
      "repas": ["Plat", …],
      "base": ["Viande", …],
      "tags": ["Pâtes", …],
      "moment": "Les deux",
      "base_servings": 1,
      "url": "…",              (optionnel)
      "image": "https://…",    (optionnel)
      "ingredients": ["70 g pâtes", …],
      "instructions": "étape 1\nétape 2\n…"
    }

SÛRETÉ :
  - Idempotent : ne recrée pas une recette dont le nom existe déjà dans la base.
  - Dry-run par défaut : logue ce qui SERAIT créé. `--apply` pour créer.

Usage :
    python -m scripts.insert_recettes            # dry-run
    python -m scripts.insert_recettes --apply    # insère les recettes manquantes
    python -m scripts.insert_recettes --file autre.json --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from app.config import settings
from app.notion_client import NotionClient
from app.text_utils import parse_ingredient_line

logger = logging.getLogger("insert_recettes")

DEFAULT_JSON = Path(__file__).parent / "recettes_a_inserer.json"


def load_recipes(path: str | Path = DEFAULT_JSON) -> list[dict[str, Any]]:
    """Charge et valide la liste de recettes du JSON. Lève si le format est invalide."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("Le JSON doit être une LISTE de recettes.")
    for r in data:
        if not isinstance(r, dict) or not str(r.get("nom", "")).strip():
            raise ValueError(f"Recette invalide (nom manquant) : {r!r}")
    return data


def structured_ingredients(lignes: list[str]) -> list[dict[str, Any]]:
    """Parse les lignes d'ingrédients en dicts structurés {nom, quantite, unite}."""
    out: list[dict[str, Any]] = []
    for ligne in lignes:
        ing = parse_ingredient_line(ligne)
        if ing and ing.get("nom"):
            out.append({"nom": ing["nom"], "quantite": ing.get("quantite", ""),
                        "unite": ing.get("unite", "")})
    return out


async def run(apply: bool, path: str | Path = DEFAULT_JSON) -> dict[str, int]:
    if not settings.notion_token:
        raise SystemExit("NOTION_TOKEN manquant : configure l'environnement.")
    recipes = load_recipes(path)
    notion = NotionClient()

    # Réutilise le cache local et le formatage Notion de l'app.
    from app.main import db as app_db, _ingredients_to_text

    existing = {r["nom"].strip().lower() for r in await notion.get_all_recipes()}
    to_create = [r for r in recipes if r["nom"].strip().lower() not in existing]
    already = len(recipes) - len(to_create)

    mode = "APPLY" if apply else "DRY-RUN"
    logger.info("=== Insertion recettes — %s === (%d déjà présentes, %d à créer)",
                mode, already, len(to_create))

    counters = {"total": len(recipes), "created": 0, "skipped": already, "errors": 0}
    for r in to_create:
        nom = r["nom"].strip()
        base_servings = int(r.get("base_servings") or 4)
        structured = structured_ingredients(r.get("ingredients", []))
        logger.info("%s « %s » [%s pers] — %d ingrédient(s)",
                    "[dry-run]" if not apply else "[apply]", nom, base_servings,
                    len(structured))
        if not apply:
            continue
        try:
            res = await notion.create_recipe(
                nom=nom,
                url=r.get("url", "") or "",
                repas=r.get("repas", []),
                tags=r.get("tags", []) or None,
                moment=r.get("moment", "") or "",
                base=r.get("base", []),
                base_servings=base_servings,
            )
            page_id = res.get("id", "")
            if not page_id:
                logger.warning("Pas d'id retourné pour « %s », on saute la suite.", nom)
                counters["errors"] += 1
                continue
            if structured:
                await notion.update_ingredients(page_id, _ingredients_to_text(structured))
                await app_db.save_enriched(
                    page_id, nom, ingredients=json.dumps(structured, ensure_ascii=False),
                    base_servings=base_servings,
                )
            if r.get("instructions"):
                await notion.rewrite_recipe_body(page_id, r["instructions"])
            if r.get("image"):
                await notion.update_image(page_id, r["image"])
            counters["created"] += 1
        except Exception:
            logger.exception("Insertion échouée pour « %s »", nom)
            counters["errors"] += 1

    logger.info("=== Résumé : %d total, %d créées, %d ignorées, %d erreurs ===",
                counters["total"], counters["created"], counters["skipped"], counters["errors"])
    if not apply and to_create:
        logger.info("Dry-run terminé. Relance avec --apply pour insérer.")
    return counters


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Insère des recettes dans Notion depuis un JSON.")
    parser.add_argument("--apply", action="store_true", help="Insère réellement (sinon dry-run).")
    parser.add_argument("--file", default=str(DEFAULT_JSON), help="Chemin du JSON à charger.")
    args = parser.parse_args()
    asyncio.run(run(args.apply, args.file))


if __name__ == "__main__":
    main()
