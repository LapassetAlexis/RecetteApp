"""Routes de la liste de courses : génération, coches, partage public."""

import json
import logging
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.categories import categorize, group_by_rayon
from app.deps import db, templates
from app.services.planning import _day_labels, _per_day_list, _repas_libres
from app.services.shopping import _collect_shopping
from app.text_utils import merge_ingredients

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/generate-shopping/{planning_id}")
async def generate_shopping(planning_id: int, request: Request):
    """Génère la liste de courses pour un planning existant."""
    try:
        planning = await db.get_planning_with_recipes(planning_id)
        if not planning:
            return {"error": "Planning introuvable"}

        planning_data = json.loads(planning["data_json"])
        plats = planning_data.get("plats", [])

        if not plats:
            return {"error": "Aucun plat dans ce planning"}

        # Ingrédients ANCRÉS sur le cache DB (pas d'extraction LLM à l'aveugle),
        # mis à l'échelle selon le nb de personnes de chaque jour.
        per_day = _per_day_list(planning, planning_data)
        collected, non_enrichis = await _collect_shopping(
            plats, per_day, planning["nb_personnes"],
        )
        # Fusion déterministe (dédup + addition par nom+unité, union des recettes).
        liste_courses = merge_ingredients(collected)

        # Ajouter les ingrédients forcés
        force = planning.get("ingredients_force", "")
        if force:
            for f in [i.strip() for i in force.split(",") if i.strip()]:
                if f.lower() not in {i["nom"].lower() for i in liste_courses}:
                    liste_courses.append({"nom": f, "quantite": "", "unite": "", "recettes": []})

        # Rayon de magasin pour chaque ingrédient (pour le groupement à l'affichage)
        for it in liste_courses:
            it["rayon"] = categorize(it.get("nom", ""))

        # Sauvegarder la liste de courses dans le planning.
        # NB : on n'écrit PAS cette liste dans Notion par recette — c'est une
        # liste agrégée/dédupliquée de la semaine, pas les ingrédients d'une
        # recette donnée (ceux-ci sont gérés à l'ajout / via enrich-all).
        planning_data["liste_courses"] = liste_courses
        # Nouveaux items : on repart d'une liste vierge de cases cochées.
        # (Le share_token éventuel est conservé tel quel.)
        planning_data["courses_checked"] = []
        await db.update_planning_data(planning_id, json.dumps(planning_data, ensure_ascii=False))

        return {
            "success": True,
            "liste_courses": liste_courses,
            "non_enrichis": sorted(set(non_enrichis)),
        }

    except Exception as e:
        logger.exception("Erreur génération liste courses")
        return {"error": str(e)}


@router.post("/api/shopping-check/{planning_id}")
async def shopping_check(planning_id: int, request: Request):
    """Coche/décoche un item de la liste de courses côté serveur.

    Body JSON : {"item": "<nom en minuscules>", "checked": true|false}.
    L'état est persisté dans planning_data["courses_checked"] (liste de clés)."""
    try:
        planning = await db.get_planning_with_recipes(planning_id)
        if not planning:
            return {"error": "Planning introuvable"}

        body = await request.json()
        item = str(body.get("item", "")).strip().lower()
        checked = bool(body.get("checked"))
        if not item:
            return {"error": "Item manquant"}

        planning_data = json.loads(planning["data_json"])
        # set pour dédupliquer, on repart de l'existant
        checked_set = set(planning_data.get("courses_checked", []))
        if checked:
            checked_set.add(item)
        else:
            checked_set.discard(item)
        planning_data["courses_checked"] = sorted(checked_set)
        await db.update_planning_data(planning_id, json.dumps(planning_data, ensure_ascii=False))
        return {"success": True}

    except Exception as e:
        logger.exception("Erreur mise à jour case liste de courses")
        return {"error": str(e)}


@router.post("/planning/{planning_id}/partager")
async def partager_liste(planning_id: int):
    """Génère (une fois) un token de partage public et renvoie l'URL relative."""
    try:
        planning = await db.get_planning_with_recipes(planning_id)
        if not planning:
            return {"error": "Planning introuvable"}

        planning_data = json.loads(planning["data_json"])
        token = planning_data.get("share_token")
        if not token:
            token = secrets.token_urlsafe(8)
            planning_data["share_token"] = token
            await db.update_planning_data(planning_id, json.dumps(planning_data, ensure_ascii=False))
        return {"url": f"/courses/{token}"}

    except Exception as e:
        logger.exception("Erreur génération lien de partage")
        return {"error": str(e)}


@router.get("/courses/{token}", response_class=HTMLResponse)
async def courses_public(request: Request, token: str):
    """Liste de courses publique (lecture seule) : n'affiche que les items NON
    cochés, groupés par rayon. Accès par token (exempté de l'authentification)."""
    planning = await db.get_planning_by_share_token(token)
    if not planning:
        return HTMLResponse("Liste introuvable", status_code=404)

    data = json.loads(planning["data_json"])
    liste_courses = data.get("liste_courses", [])
    checked = {str(c).lower() for c in data.get("courses_checked", [])}
    restants = [it for it in liste_courses if it.get("nom", "").lower() not in checked]

    return templates.TemplateResponse(
        request,
        "courses_public.html",
        {
            "courses_par_rayon": group_by_rayon(restants),
            "repas_libres": _repas_libres(
                data.get("plats", []), _day_labels(planning.get("week_start", "")),
            ),
            "nb_restants": len(restants),
            "nb_total": len(liste_courses),
        },
    )
