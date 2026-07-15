"""Migration Notion : crée la propriété « Portions » (number) dans la base.

Objectif : introduire une base de portions PAR recette. Le champ « Portions »
(number) porte le nombre de personnes pour lesquelles les quantités
d'ingrédients d'une recette sont exprimées.

SÛRETÉ MAXIMALE :
  - Idempotent : re-run sans effet si la propriété existe déjà.
  - NE remplit AUCUNE valeur : les recettes sans « Portions » sont lues comme 4
    (défaut), ce qui reproduit l'ancien comportement global BASE_SERVINGS=4.
  - Dry-run par défaut : logue ce qui CHANGERAIT. `--apply` pour écrire.

Usage :
    python -m scripts.add_portions            # dry-run (aucune écriture)
    python -m scripts.add_portions --apply    # crée la propriété si absente
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

import httpx

from app.config import settings
from app.notion_client import BASE_URL, NOTION_VERSION

logger = logging.getLogger("add_portions")


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.notion_token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


async def _ensure_portions(client: httpx.AsyncClient, apply: bool) -> None:
    """Crée la propriété « Portions » (number) si absente. Ne touche à rien d'autre."""
    resp = await client.get(
        f"{BASE_URL}/databases/{settings.notion_database_id}",
        headers=_headers(), timeout=30,
    )
    resp.raise_for_status()
    props = resp.json().get("properties", {})

    if "Portions" in props:
        logger.info("Schéma : « Portions » existe déjà, rien à créer.")
        return

    update: dict[str, Any] = {"Portions": {"number": {"format": "number"}}}
    logger.info("Schéma : créer la propriété « Portions » (number).")

    if not apply:
        logger.info("[dry-run] La propriété « Portions » serait créée. "
                    "Relance avec --apply pour écrire.")
        return

    patch = await client.patch(
        f"{BASE_URL}/databases/{settings.notion_database_id}",
        headers=_headers(), json={"properties": update}, timeout=30,
    )
    patch.raise_for_status()
    logger.info("Propriété « Portions » créée.")


async def run(apply: bool) -> None:
    if not settings.notion_token:
        raise SystemExit("NOTION_TOKEN manquant : configure l'environnement avant de lancer la migration.")
    mode = "APPLY (écriture réelle)" if apply else "DRY-RUN (aucune écriture)"
    logger.info("=== Migration Portions — %s ===", mode)
    async with httpx.AsyncClient() as client:
        await _ensure_portions(client, apply)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Crée la propriété Notion « Portions » (number).")
    parser.add_argument("--apply", action="store_true",
                        help="Crée réellement la propriété (sinon dry-run).")
    args = parser.parse_args()
    asyncio.run(run(args.apply))


if __name__ == "__main__":
    main()
