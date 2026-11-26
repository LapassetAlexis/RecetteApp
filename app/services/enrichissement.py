"""Enrichissement des recettes par lot : ingrédients du cache local (ou
extraits par le LLM depuis l'URL source) recopiés dans Notion, pour les
recettes d'un planning ou tout le catalogue (flux SSE de progression)."""

import json
import logging

from app.deps import db, llm, notion
from app.services.planning import plat_accompagnements
from app.services.shopping import ing_lines
from app.text_utils import normalize_title_case

logger = logging.getLogger(__name__)


def planning_recipes(plats: list[dict]) -> list[dict]:
    """Recettes uniques d'un planning (plats + accompagnements, dédup par
    notion_id), au format attendu par enrich_one : {id, nom, url}."""
    seen: set[str] = set()
    recettes: list[dict] = []
    for p in plats:
        for src in (p, *plat_accompagnements(p)):
            nid = src.get("notion_id")
            if nid and nid not in seen:
                seen.add(nid)
                recettes.append({"id": nid, "nom": src.get("nom_recette", ""),
                                 "url": src.get("url", "")})
    return recettes


async def enrich_one(r: dict) -> str:
    """Enrichit une recette Notion. Retourne 'enriched' | 'skipped' | 'error'."""
    nid, nom = r.get("id"), r.get("nom")
    if not nid or not nom:
        return "skipped"
    # Normalise un titre tout en MAJUSCULES (best-effort, n'empêche pas l'enrichissement)
    nom_norm = normalize_title_case(nom)
    if nom_norm != nom:
        try:
            await notion.update_recipe_title(nid, nom_norm)
            nom = nom_norm
        except Exception as e:
            logger.warning(f"Normalisation titre échouée pour {nom}: {e}")
    ingredients_txt = ""
    cached = await db.get_enriched(nid)
    if cached and cached.get("ingredients"):
        try:
            ing_list = json.loads(cached["ingredients"])
            if ing_list:
                ingredients_txt = ing_lines(ing_list)
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            logger.warning(f"Cache ingrédients illisible pour {nom}: {e}")
    if not ingredients_txt and r.get("url"):
        try:
            d = await llm.extract_ingredients(nom, r.get("url", ""))
            ings = d.get("ingredients", [])
            if ings:
                ingredients_txt = ing_lines(ings)
                await db.save_enriched(nid, nom, ingredients=json.dumps(ings))
        except Exception as e:
            logger.warning(f"Extraction LLM échouée pour {nom}: {e}")
    if not ingredients_txt:
        return "skipped"
    try:
        await notion.update_ingredients(nid, ingredients_txt)
        return "enriched"
    except Exception as e:
        logger.warning(f"Erreur écriture pour {nom}: {e}")
        return "error"


async def enrich_many(recettes: list[dict]) -> dict[str, int]:
    """Enrichit une liste de recettes et compte les issues."""
    counts = {"enriched": 0, "skipped": 0, "errors": 0}
    for r in recettes:
        status = await enrich_one(r)
        counts["errors" if status == "error" else status] += 1
    return counts


async def enrich_all_events():
    """Événements SSE de l'enrichissement de tout le catalogue : progression
    après chaque recette, puis `done` (ou `error`)."""
    try:
        recettes = await notion.get_all_recipes()
        total = len(recettes)
        counts = {"enriched": 0, "skipped": 0, "errors": 0}
        yield f"data: {json.dumps({'total': total, 'done': 0})}\n\n"
        for i, r in enumerate(recettes, 1):
            status = await enrich_one(r)
            counts["errors" if status == "error" else status] += 1
            payload = {"total": total, "done": i, "nom": r.get("nom", ""), "status": status, **counts}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        yield f"event: done\ndata: {json.dumps({'total': total, **counts})}\n\n"
    except Exception as e:
        logger.exception("Erreur enrichissement masse (stream)")
        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
