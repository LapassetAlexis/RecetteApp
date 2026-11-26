"""Sauvegarde périodique de la base locale (à lancer en cron dans le conteneur).

Écrit `menu-planner-AAAA-MM-JJ-HHMMSS.json` (même format que l'export de la
page « Données ») dans le dossier cible, puis ne garde que les N plus récents.

Usage :
    python -m scripts.backup /data/backups            # garde 8 fichiers
    python -m scripts.backup /data/backups --keep 14

Exemple de crontab (hôte) :
    0 3 * * * docker exec -u appuser menu-planner python -m scripts.backup /data/backups
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from app.database import Database
from app.services.backup import export_all

logger = logging.getLogger("backup")

PREFIX = "menu-planner-"
DEFAULT_KEEP = 8


def rotate(directory: Path, keep: int) -> list[Path]:
    """Supprime les sauvegardes les plus anciennes au-delà de `keep`.

    Le nom horodaté se trie dans l'ordre chronologique. Renvoie les fichiers supprimés."""
    files = sorted(directory.glob(f"{PREFIX}*.json"))
    removed = files[:-keep] if keep > 0 else files
    for f in removed:
        f.unlink()
    return removed


async def run(directory: Path, keep: int = DEFAULT_KEEP, now: datetime | None = None) -> Path:
    """Exporte la base dans `directory` (écriture atomique) puis applique la rotation."""
    directory.mkdir(parents=True, exist_ok=True)
    db = Database()
    await db.init()
    data = await export_all(db)
    target = directory / f"{PREFIX}{(now or datetime.now()):%Y-%m-%d-%H%M%S}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, target)
    for f in rotate(directory, keep):
        logger.info("Ancienne sauvegarde supprimée : %s", f.name)
    logger.info("Sauvegarde écrite : %s (%d plannings, %d recettes en cache)",
                target, len(data["plannings"]), len(data["enriched"]))
    return target


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Sauvegarde JSON de la base Menu Planner")
    parser.add_argument("directory", type=Path, help="dossier de destination (ex. /data/backups)")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                        help=f"nombre de sauvegardes conservées (défaut {DEFAULT_KEEP})")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(run(args.directory, max(1, args.keep)))


if __name__ == "__main__":
    main()
