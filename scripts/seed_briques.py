"""Peuple la base Notion avec une bibliothèque de « briques » de départ.

Une brique = composant simple (Nature=Ingrédient) avec une Base (Viande/
Poisson/Œuf/Féculent/Légume) et un ingrédient de courses = son propre nom + une
quantité par personne (qui sera mise à l'échelle selon le nb de personnes).

SÛRETÉ :
  - Idempotent : ne recrée pas une brique dont le nom existe déjà dans la base.
  - Dry-run par défaut : logue ce qui SERAIT créé. `--apply` pour créer.

Usage :
    python -m scripts.seed_briques          # dry-run
    python -m scripts.seed_briques --apply   # crée les briques manquantes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from app.config import settings
from app.notion_client import NotionClient
from app.text_utils import parse_ingredient_line

logger = logging.getLogger("seed_briques")

# (nom, base, quantité, unité) — quantité PAR PERSONNE.
_BRIQUES: list[tuple[str, str, str, str]] = [
    # Viande
    ("Steak de bœuf", "Viande", "150", "g"),
    ("Escalope de poulet", "Viande", "150", "g"),
    ("Steak haché", "Viande", "150", "g"),
    ("Jambon", "Viande", "2", "tranches"),
    ("Saucisse", "Viande", "2", "pièces"),
    # Poisson
    ("Filet de saumon", "Poisson", "150", "g"),
    ("Filet de cabillaud", "Poisson", "150", "g"),
    ("Crevettes", "Poisson", "100", "g"),
    ("Thon", "Poisson", "1", "boîte"),
    # Œuf
    ("Œufs", "Œuf", "2", "pièces"),
    # Féculent
    ("Riz", "Féculent", "80", "g"),
    ("Pâtes", "Féculent", "80", "g"),
    ("Semoule", "Féculent", "60", "g"),
    ("Quinoa", "Féculent", "70", "g"),
    ("Pommes de terre", "Féculent", "200", "g"),
    # Légume
    ("Haricots verts", "Légume", "200", "g"),
    ("Brocolis", "Légume", "200", "g"),
    ("Chou-fleur", "Légume", "200", "g"),
    ("Courgettes", "Légume", "200", "g"),
    ("Épinards", "Légume", "200", "g"),
    ("Carottes", "Légume", "150", "g"),
    ("Ratatouille", "Légume", "200", "g"),
    ("Salade verte", "Légume", "1", "pièce"),
    ("Petits pois", "Légume", "150", "g"),
]


async def run(apply: bool) -> None:
    if not settings.notion_token:
        raise SystemExit("NOTION_TOKEN manquant : configure l'environnement.")
    notion = NotionClient()

    existing = {r["nom"].strip().lower() for r in await notion.get_all_recipes()}
    to_create = [b for b in _BRIQUES if b[0].strip().lower() not in existing]
    already = len(_BRIQUES) - len(to_create)

    mode = "APPLY" if apply else "DRY-RUN"
    logger.info("=== Seed briques — %s === (%d déjà présentes, %d à créer)",
                mode, already, len(to_create))

    # Réutilise le cache local et le formatage Notion de l'app.
    from app.main import db as app_db, _ingredients_to_text

    created = 0
    for nom, base, qte, unite in to_create:
        ing = parse_ingredient_line(f"{qte} {unite} {nom}") or {"nom": nom, "quantite": qte, "unite": unite}
        structured = [{"nom": ing["nom"], "quantite": ing["quantite"], "unite": ing["unite"]}]
        logger.info("%s brique « %s » [%s] — %s %s",
                    "[dry-run]" if not apply else "[apply]", nom, base, qte, unite)
        if not apply:
            continue
        try:
            res = await notion.create_recipe(nom=nom, nature="Ingrédient", base=[base])
            page_id = res.get("id", "")
            if page_id:
                await notion.update_ingredients(page_id, _ingredients_to_text(structured))
                await app_db.save_enriched(page_id, nom, ingredients=json.dumps(structured))
            created += 1
        except Exception:
            logger.exception("Création échouée pour « %s »", nom)

    logger.info("=== Résumé : %d à créer, %d créées ===",
                len(to_create), created if apply else 0)
    if not apply and to_create:
        logger.info("Dry-run terminé. Relance avec --apply pour créer.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Seed des briques de base dans Notion.")
    parser.add_argument("--apply", action="store_true", help="Crée réellement (sinon dry-run).")
    args = parser.parse_args()
    asyncio.run(run(args.apply))


if __name__ == "__main__":
    main()
