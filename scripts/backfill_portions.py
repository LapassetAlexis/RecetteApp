"""Backfill « best-effort » de la propriété Notion « Portions » des recettes
EXISTANTES, depuis le `recipeYield` du JSON-LD de leur URL source.

Objectif : donner une base de portions réaliste aux recettes déjà en base qui
n'en ont pas, sans jamais écraser un réglage choisi à la main.

CHOIX DE SÛRETÉ (ne jamais réduire un réglage explicite) :
  On relit la page Notion à l'état BRUT pour distinguer « Portions réellement
  absente » (number == null) d'une valeur explicitement posée (même 4). On ne
  touche QU'AUX recettes dont « Portions » est null. Toute valeur déjà écrite
  (manuelle ou issue d'un précédent backfill) est laissée telle quelle → le
  script est idempotent et ne dégrade jamais un choix utilisateur.

SÛRETÉ MAXIMALE :
  - Idempotent : re-run sans effet sur les recettes déjà renseignées.
  - Dry-run par défaut : logue ce qui CHANGERAIT. `--apply` pour écrire.
  - Erreurs isolées par recette : un fetch qui échoue (404, timeout…) → skip,
    n'arrête pas le parcours.

Usage :
    python -m scripts.backfill_portions            # dry-run (aucune écriture)
    python -m scripts.backfill_portions --apply    # écrit les Portions trouvées
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from typing import Any

import httpx

from app.config import settings
from app.llm_client import _jsonld_candidates, _safe_fetch_html
from app.notion_client import BASE_URL, NOTION_VERSION, NotionClient

logger = logging.getLogger("backfill_portions")


# ── Logique PURE (testable sans réseau) ──────────────────────────────

def parse_yield(raw: Any) -> int | None:
    """Extrait un nombre de portions PLAUSIBLE (1..12) d'un `recipeYield` brut.

    Gère : 4, "4", "4 portions", "Pour 6 personnes", ["4"], une liste de valeurs,
    "4-6" (prend le 1er), "" / None / "quelques" (→ None). Retourne le PREMIER
    entier trouvé dans 1..12, sinon None.
    """
    if raw is None:
        return None
    # Listes / tuples : on tente chaque élément dans l'ordre.
    if isinstance(raw, (list, tuple)):
        for item in raw:
            n = parse_yield(item)
            if n is not None:
                return n
        return None
    if isinstance(raw, bool):  # bool est un int en Python : on l'exclut.
        return None
    if isinstance(raw, (int, float)):
        n = int(raw)
        return n if 1 <= n <= 12 else None
    if isinstance(raw, str):
        m = re.search(r"\d+", raw)
        if not m:
            return None
        n = int(m.group())
        return n if 1 <= n <= 12 else None
    return None


def _yield_from_html(html_text: str) -> Any:
    """Renvoie la valeur brute `recipeYield` de la 1re recette JSON-LD, ou None."""
    if not html_text:
        return None
    for block in _jsonld_candidates(html_text):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        candidates: list[Any] = []
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            candidates = [data]
            graph = data.get("@graph")
            if isinstance(graph, list):
                candidates += graph
        for node in candidates:
            if not isinstance(node, dict):
                continue
            t = node.get("@type", "")
            types = t if isinstance(t, list) else [t]
            if not any("Recipe" in str(x) for x in types):
                continue
            y = node.get("recipeYield")
            if y is None:
                y = node.get("yield")
            return y
    return None


# ── Réseau ───────────────────────────────────────────────────────────

def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.notion_token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


async def _fetch_raw_pages(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Parcourt toute la base et renvoie {id, nom, url, portions} par page.

    `portions` = valeur BRUTE de la propriété number « Portions » (None si la
    propriété est absente / vide → seule cible du backfill)."""
    out: list[dict[str, Any]] = []
    start_cursor: str | None = None
    while True:
        body: dict[str, Any] = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor
        resp = await client.post(
            f"{BASE_URL}/databases/{settings.notion_database_id}/query",
            headers=_headers(), json=body, timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for page in data.get("results", []):
            p = page.get("properties", {})
            title = p.get("Nom", {}).get("title") or []
            nom = title[0].get("plain_text", "") if title else ""
            if not nom:
                continue
            out.append({
                "id": page["id"],
                "nom": nom,
                "url": p.get("URL", {}).get("url") or "",
                "portions": p.get("Portions", {}).get("number"),
            })
        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")
    return out


async def run(apply: bool) -> dict[str, int]:
    if not settings.notion_token:
        raise SystemExit("NOTION_TOKEN manquant : configure l'environnement avant de lancer le backfill.")
    mode = "APPLY (écriture réelle)" if apply else "DRY-RUN (aucune écriture)"
    logger.info("=== Backfill Portions — %s ===", mode)

    notion = NotionClient()
    # Cache local (best-effort) : réutilise l'instance db de l'app.
    from app.main import db as app_db

    async with httpx.AsyncClient() as client:
        pages = await _fetch_raw_pages(client)

    counters = {"total": len(pages), "updated": 0, "skipped": 0, "errors": 0}
    for page in pages:
        nom = page["nom"]
        # 1) Réglage explicite (même 4) → on ne touche JAMAIS.
        if page["portions"] is not None:
            logger.info("%s → skip (Portions déjà réglé : %s)", nom, page["portions"])
            counters["skipped"] += 1
            continue
        # 2) Pas d'URL source → rien à extraire.
        if not page["url"]:
            logger.info("%s → skip (pas d'URL)", nom)
            counters["skipped"] += 1
            continue
        # 3) Fetch + extraction du recipeYield (best-effort, erreurs isolées).
        try:
            html_text = await _safe_fetch_html(page["url"])
            n = parse_yield(_yield_from_html(html_text))
        except Exception:
            logger.exception("%s → erreur (fetch/parse)", nom)
            counters["errors"] += 1
            continue
        if n is None:
            logger.info("%s → skip (pas de yield exploitable) [%s]", nom, page["url"])
            counters["skipped"] += 1
            continue

        logger.info("%s → yield=%d", nom, n)
        counters["updated"] += 1
        if not apply:
            continue
        try:
            await notion.update_portions(page["id"], n)
            # Cache local : uniquement si la recette y est DÉJÀ (on ne crée pas
            # d'entrée cache ici — l'important est Notion).
            cached = await app_db.get_enriched(page["id"])
            if cached:
                await app_db.save_enriched(
                    page["id"], cached.get("recipe_name") or nom,
                    ingredients=cached.get("ingredients") or "",
                    cuisson_minutes=cached.get("cuisson_minutes") or 0,
                    saison=cached.get("saison") or "",
                    nutrition=cached.get("nutrition") or "",
                    base_servings=n,
                )
        except Exception:
            logger.exception("%s → erreur (écriture Notion/cache)", nom)
            counters["errors"] += 1
            counters["updated"] -= 1

    logger.info(
        "=== Résumé : %d recettes, %d mises à jour, %d ignorées, %d erreurs ===",
        counters["total"], counters["updated"], counters["skipped"], counters["errors"],
    )
    if not apply and counters["updated"]:
        logger.info("Dry-run terminé. Relance avec --apply pour écrire ces Portions.")
    return counters


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        description="Backfill des Portions Notion depuis le recipeYield des URLs sources.")
    parser.add_argument("--apply", action="store_true",
                        help="Écrit réellement les Portions trouvées (sinon dry-run).")
    args = parser.parse_args()
    asyncio.run(run(args.apply))


if __name__ == "__main__":
    main()
