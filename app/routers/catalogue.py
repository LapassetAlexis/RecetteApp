"""Catalogue des recettes pour le picker du constructeur."""

import logging

from fastapi import APIRouter

from app.config import recipe_base, recipe_nature, recipe_types
from app.deps import notion
from app.text_utils import clean_recipe_title

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/catalogue")
async def api_catalogue():
    """Catalogue complet des recettes Notion pour le picker du constructeur.
    Renvoie [{id, nom, nature, base:[...], repas:[...]}] pour toutes les recettes."""
    try:
        recettes = await notion.get_all_recipes()
    except Exception as e:
        logger.error(f"Erreur Notion (catalogue): {e}")
        return []
    return [
        {
            "id": r["id"],
            "nom": clean_recipe_title(r["nom"]),
            "nature": recipe_nature(r),
            "base": recipe_base(r),
            "repas": recipe_types(r),
            "image": r.get("image", ""),
        }
        for r in recettes
    ]
