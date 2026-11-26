"""Mode cuisine : une recette en plein écran, étape par étape, ingrédients
cochables et minuteurs (le comportement est côté client, `js/cuisine.js`)."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import deps
from app.services.recettes import _cached_ingredients, _detail_base_servings
from app.services.shopping import fmt_quantite
from app.text_utils import scale_ingredients

logger = logging.getLogger(__name__)
router = APIRouter()
# Instances partagées lues via `deps.` à chaque requête (et non importées par
# nom) : les tests qui rechargent `app.deps` restent cohérents sans recharger
# ce routeur.

MAX_PORTIONS = 50


def _portions(raw: str | None, base: int) -> int:
    """Nombre de portions demandé, borné à 1..MAX_PORTIONS ; repli sur la base
    de la recette si absent ou illisible."""
    try:
        n = int(raw) if raw not in (None, "") else base
    except (TypeError, ValueError):
        n = base
    return max(1, min(MAX_PORTIONS, n))


@router.get("/recette/{page_id}/cuisine", response_class=HTMLResponse)
async def mode_cuisine(request: Request, page_id: str, portions: str | None = None):
    """Page plein écran sans navigation : ingrédients mis à l'échelle de
    `portions` et étapes une à une."""
    try:
        recette = await deps.notion.get_recipe(page_id)
        if not recette:
            return HTMLResponse("Recette non trouvée", status_code=404)
        cached = await deps.db.get_enriched(page_id)
        base = _detail_base_servings(cached, recette)
        n = _portions(portions, base)
        # Virgule décimale française à l'affichage (« 1,5 kg »), comme les courses.
        ingredients = [{**i, "quantite": fmt_quantite(i.get("quantite"))}
                       for i in scale_ingredients(_cached_ingredients(cached), n / base)]
        try:
            instructions = await deps.notion.get_recipe_instructions(page_id)
        except Exception as e:
            logger.warning(f"Instructions illisibles pour {page_id}: {e}")
            instructions = []
        return deps.templates.TemplateResponse(
            request,
            "cuisine.html",
            {
                "recette": recette,
                "ingredients": ingredients,
                "instructions": instructions,
                "portions": n,
            },
        )
    except Exception:
        logger.exception("Erreur mode cuisine")
        return HTMLResponse("Erreur interne lors du chargement de la recette.", status_code=500)
