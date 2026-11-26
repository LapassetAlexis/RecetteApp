"""Règles métier des recettes : statistiques de la liste, données de la fiche
(ingrédients en cache, portions, nutrition) et enrichissement par lot."""

import json
import logging

from app.config import (
    BASE_OPTIONS,
    NATURE_OPTIONS,
    REPAS_OPTIONS,
    TAG_OPTIONS,
    recipe_base,
    recipe_nature,
    recipe_types,
)
from app.deps import BASE_SERVINGS, db, llm, notion
from app.nutrition import estimate_nutrition
from app.services.planning import plat_accompagnements
from app.services.shopping import _ing_lines
from app.text_utils import normalize_title_case

logger = logging.getLogger(__name__)

_NUTRITION_KEYS = ("calories", "proteines", "glucides", "lipides")


def _form_options() -> dict:
    """Valeurs proposées par les formulaires d'ajout et d'édition de recette."""
    return {
        "repas_options": REPAS_OPTIONS,
        "tag_options": TAG_OPTIONS,
        "base_options": BASE_OPTIONS,
        "nature_options": NATURE_OPTIONS,
    }


def _recipe_stats(recettes: list[dict], ings_by_id: dict[str, str],
                  duree_by_id: dict[str, int]) -> dict:
    """Compteurs des filtres de la liste (type / base / état / tag / nature).

    Reporte aussi sur chaque recette ses ingrédients en cache (recherche par
    ingrédient) et sa durée (badge temps)."""
    par_type: dict[str, int] = {}
    par_base: dict[str, int] = {}
    par_etat: dict[str, int] = {}
    par_tag: dict[str, int] = {}
    par_nature: dict[str, int] = {}
    for r in recettes:
        r["ingredients_search"] = ings_by_id.get(r["id"], "")
        r["duree"] = duree_by_id.get(r["id"], 0)
        par_nature[recipe_nature(r)] = par_nature.get(recipe_nature(r), 0) + 1
        types = recipe_types(r) or ["Non classé"]
        for t in types:
            par_type[t] = par_type.get(t, 0) + 1
        for b in recipe_base(r):
            par_base[b] = par_base.get(b, 0) + 1
        if r["etat"]:
            par_etat[r["etat"]] = par_etat.get(r["etat"], 0) + 1
        for t in r["tags"]:
            par_tag[t] = par_tag.get(t, 0) + 1
    return {
        "total": len(recettes),
        "par_nature": par_nature,
        "par_type": sorted(par_type.items()),
        "par_base": sorted(par_base.items(), key=lambda kv: -kv[1]),
        "par_etat": sorted(par_etat.items()),
        "par_tag": sorted(par_tag.items(), key=lambda kv: -kv[1]),  # tags par fréquence
    }


def _cached_ingredients(cached: dict | None) -> list[dict]:
    """Ingrédients structurés {nom, quantite (texte), unite} du cache local."""
    if not cached or not cached.get("ingredients"):
        return []
    try:
        return [
            {"nom": i.get("nom", ""), "quantite": str(i.get("quantite", "") or ""),
             "unite": i.get("unite", "")}
            for i in json.loads(cached["ingredients"]) if i.get("nom")
        ]
    except (json.JSONDecodeError, TypeError):
        return []


def _detail_base_servings(cached: dict | None, recette: dict) -> int:
    """Base de portions d'une recette : priorité au cache, repli sur la valeur
    Notion (Portions) lue par _parse_page, défaut BASE_SERVINGS."""
    base_servings = BASE_SERVINGS
    try:
        if cached and cached.get("base_servings"):
            base_servings = int(cached["base_servings"])
        elif recette.get("base_servings"):
            base_servings = int(recette["base_servings"])
    except (TypeError, ValueError):
        base_servings = BASE_SERVINGS
    if base_servings <= 0:
        base_servings = BASE_SERVINGS
    return base_servings


def _detail_nutrition(cached: dict | None, ingredients: list[dict], base_servings: int) -> dict | None:
    """Nutrition par part : valeurs exactes de la source (JSON-LD) si on les a
    stockées, sinon estimation calculée depuis les ingrédients."""
    if cached and cached.get("nutrition"):
        try:
            src = json.loads(cached["nutrition"]) or {}
        except (json.JSONDecodeError, TypeError):
            src = {}
        # n'utiliser la nutrition de la source que si COMPLÈTE (calories +
        # 3 macros) ; sinon (souvent juste calories, parfois fausses) on estime
        if src and all(src.get(k) is not None for k in _NUTRITION_KEYS):
            src.setdefault("source", "source")
            src.setdefault("confiance", "Excellente")  # valeurs officielles du site
            return src
    if ingredients:
        return estimate_nutrition(ingredients, base_servings)
    return None


def _analysis_response(info: dict, image_url: str, source: str) -> dict:
    """Réponse des analyses d'URL ou de texte : champs pré-remplis du formulaire
    d'ajout (les ingrédients en lignes brutes sont joints en texte)."""
    ings = info.get("ingredients", [])
    ingredients = "\n".join(ings) if isinstance(ings, list) else str(ings)
    return {
        "nom": info.get("nom", ""),
        "repas": info.get("type_repas", ""),
        "tags": info.get("tags", []),
        "base": info.get("base", []),
        "ingredients": ingredients,
        "instructions": info.get("instructions", ""),
        "image_url": image_url,
        "moment": "Les deux",
        "source": source,
    }


def _planning_recipes(plats: list[dict]) -> list[dict]:
    """Recettes uniques d'un planning (plats + accompagnements, dédup par
    notion_id), au format attendu par _enrich_one : {id, nom, url}."""
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


async def _enrich_one(r: dict) -> str:
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
                ingredients_txt = _ing_lines(ing_list)
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            logger.warning(f"Cache ingrédients illisible pour {nom}: {e}")
    if not ingredients_txt and r.get("url"):
        try:
            d = await llm.extract_ingredients(nom, r.get("url", ""))
            ings = d.get("ingredients", [])
            if ings:
                ingredients_txt = _ing_lines(ings)
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


async def _enrich_many(recettes: list[dict]) -> dict[str, int]:
    """Enrichit une liste de recettes et compte les issues."""
    counts = {"enriched": 0, "skipped": 0, "errors": 0}
    for r in recettes:
        status = await _enrich_one(r)
        counts["errors" if status == "error" else status] += 1
    return counts


async def _enrich_all_events():
    """Événements SSE de l'enrichissement de tout le catalogue : progression
    après chaque recette, puis `done` (ou `error`)."""
    try:
        recettes = await notion.get_all_recipes()
        total = len(recettes)
        counts = {"enriched": 0, "skipped": 0, "errors": 0}
        yield f"data: {json.dumps({'total': total, 'done': 0})}\n\n"
        for i, r in enumerate(recettes, 1):
            status = await _enrich_one(r)
            counts["errors" if status == "error" else status] += 1
            payload = {"total": total, "done": i, "nom": r.get("nom", ""), "status": status, **counts}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        yield f"event: done\ndata: {json.dumps({'total': total, **counts})}\n\n"
    except Exception as e:
        logger.exception("Erreur enrichissement masse (stream)")
        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
