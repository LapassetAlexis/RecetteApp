"""Catalogue des recettes pour le picker du constructeur."""

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import recipe_base, recipe_nature, recipe_types
from app.deps import db, notion
from app.text_utils import clean_recipe_title

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/catalogue")
async def api_catalogue():
    """Catalogue complet des recettes Notion pour le picker du constructeur.

    Renvoie [{id, nom, nature, base, repas, image, tags, note, etat, duree,
    usage}] : `duree` = minutes du cache local (0 si inconnue), `usage` =
    {count, last} tiré des plannings validés ({count: 0, last: ""} si jamais
    planifiée). Notion en échec → 502 {"error": …} (le picker propose de
    réessayer) ; une lecture du cache local en échec n'empêche pas de répondre."""
    try:
        recettes = await notion.get_all_recipes()
    except Exception as e:
        logger.error(f"Erreur Notion (catalogue): {e}")
        return JSONResponse({"error": "Catalogue Notion indisponible."}, status_code=502)
    try:
        durees = await db.get_all_enriched_durations()
    except Exception as e:
        logger.warning(f"Lecture des durées en cache échouée: {e}")
        durees = {}
    try:
        usage = await db.get_recipe_usage()
    except Exception as e:
        logger.warning(f"Lecture de l'usage des recettes échouée: {e}")
        usage = {}
    out = []
    for r in recettes:
        u = usage.get(r["id"]) or {}
        out.append({
            "id": r["id"],
            "nom": clean_recipe_title(r["nom"]),
            "nature": recipe_nature(r),
            "base": recipe_base(r),
            "repas": recipe_types(r),
            "image": r.get("image", ""),
            "tags": list(r.get("tags") or []),
            "note": r.get("note") or "",
            "etat": r.get("etat") or "",
            "duree": durees.get(r["id"], 0),
            "usage": {"count": u.get("count", 0), "last": u.get("last", "")},
        })
    return out
