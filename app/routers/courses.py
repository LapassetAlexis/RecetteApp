"""Routes de la liste de courses : génération, coches, articles manuels,
placard et partage public (le jeton donne le droit de cocher, rien d'autre)."""

import json
import logging
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.deps import db, templates
from app.services.planning import _day_labels, _per_day_list, _repas_libres
from app.services.shopping import (
    _collect_shopping,
    add_extra,
    apply_check,
    finalize_items,
    load_shopping_view,
    mutate_planning_data,
    purge_orphans,
    recipe_item_name,
    remove_extra,
    reset_checks,
    set_masque,
    set_placard,
)
from app.text_utils import merge_ingredients

logger = logging.getLogger(__name__)
router = APIRouter()


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _body(request: Request) -> dict:
    """Corps JSON de la requête ({} s'il est absent ou illisible)."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


async def _liste_html(planning_id: int, data: dict) -> dict:
    """Réponse des actions de la page planning : la liste re-rendue côté serveur
    (même gabarit que l'affichage initial) et le nombre d'articles visibles."""
    shop = await load_shopping_view(data)
    html = templates.get_template("partials/_shopping_list.html").render(
        shop=shop, planning={"id": planning_id},
    )
    return {"success": True, "html": html, "nb": shop["nb_total"]}


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

        # Rayon de magasin et clé de chaque article (groupement et coches).
        finalize_items(liste_courses)

        # Sauvegarder la liste de courses dans le planning.
        # NB : on n'écrit PAS cette liste dans Notion par recette — c'est une
        # liste agrégée/dédupliquée de la semaine, pas les ingrédients d'une
        # recette donnée (ceux-ci sont gérés à l'ajout / via enrich-all).
        planning_data["liste_courses"] = liste_courses
        # Nouveaux items : on repart sans coche (sauf articles manuels, qui
        # survivent à la régénération), masques orphelins retirés.
        # (Le share_token éventuel est conservé tel quel.)
        reset_checks(planning_data)
        purge_orphans(planning_data)
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

    Body JSON : {"item": "<clé d'article>", "checked": true|false}. Un nom seul
    (ancien client) est encore accepté. L'état est persisté dans
    planning_data["courses_checked"] (liste de clés)."""
    try:
        body = await _body(request)
        item = str(body.get("item", "")).strip().lower()
        checked = bool(body.get("checked"))
        if not item:
            return {"error": "Item manquant"}

        data, ok = await mutate_planning_data(
            planning_id, lambda d: apply_check(d, item, checked),
        )
        if data is None:
            return {"error": "Planning introuvable"}
        # Clé inconnue (article disparu, coche rejouée hors-ligne) : rien à faire,
        # mais ce n'est pas une erreur à rejouer.
        return {"success": True, "ignored": not ok}

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


# ── Page publique : lecture + coches uniquement ─────────────────────────

@router.get("/courses/{token}", response_class=HTMLResponse)
async def courses_public(request: Request, token: str):
    """Liste de courses publique : articles à prendre groupés par rayon, les
    articles cochés repliés en bas sous « Déjà pris ». Accès par token
    (exempté de l'authentification), qui ne permet que de cocher."""
    planning = await db.get_planning_by_share_token(token)
    if not planning:
        return HTMLResponse("Liste introuvable", status_code=404)

    data = json.loads(planning["data_json"])
    return templates.TemplateResponse(
        request,
        "courses_public.html",
        {
            "shop": await load_shopping_view(data),
            "repas_libres": _repas_libres(
                data.get("plats", []), _day_labels(planning.get("week_start", "")),
            ),
        },
    )


@router.get("/courses/{token}/etat")
async def courses_public_etat(token: str):
    """État des coches pour le rafraîchissement de la page publique.

    `version` change quand les lignes de la liste changent : la page se recharge."""
    planning = await db.get_planning_by_share_token(token)
    if not planning:
        return _error("Liste introuvable", 404)
    shop = await load_shopping_view(json.loads(planning["data_json"]))
    return {"checked": shop["checked"], "version": shop["version"]}


@router.post("/courses/{token}/check")
async def courses_public_check(token: str, request: Request):
    """Coche / décoche depuis le lien public. Même corps que /api/shopping-check,
    mais seule une clé d'article existante est acceptée."""
    planning = await db.get_planning_by_share_token(token)
    if not planning:
        return _error("Liste introuvable", 404)
    body = await _body(request)
    item = str(body.get("item", ""))
    checked = bool(body.get("checked"))
    data, ok = await mutate_planning_data(
        planning["id"], lambda d: apply_check(d, item, checked, strict=True),
    )
    if data is None:
        return _error("Liste introuvable", 404)
    if not ok:
        return _error("Article inconnu", 400)
    return {"success": True}


# ── Articles manuels, masques et placard (page planning, authentifiée) ──

@router.post("/api/courses/{planning_id}/extra")
async def courses_extra_add(planning_id: int, request: Request):
    """Ajoute un article à la main : {"texte": "2 l lait"}."""
    texte = str((await _body(request)).get("texte", ""))
    data, item = await mutate_planning_data(planning_id, lambda d: add_extra(d, texte))
    if data is None:
        return _error("Planning introuvable", 404)
    if item is None:
        return _error("Article vide ou liste pleine", 400)
    return {**await _liste_html(planning_id, data), "item": item}


@router.delete("/api/courses/{planning_id}/extra/{key}")
async def courses_extra_delete(planning_id: int, key: str):
    """Retire un article ajouté à la main."""
    data, ok = await mutate_planning_data(planning_id, lambda d: remove_extra(d, key))
    if data is None:
        return _error("Planning introuvable", 404)
    if not ok:
        return _error("Article inconnu", 404)
    return await _liste_html(planning_id, data)


@router.post("/api/courses/{planning_id}/masquer")
async def courses_masquer(planning_id: int, request: Request):
    """Masque (« Je l'ai déjà ») ou restaure un article issu d'une recette :
    {"key": "...", "masque": true|false}."""
    body = await _body(request)
    key = str(body.get("key", ""))
    masque = bool(body.get("masque", True))
    data, ok = await mutate_planning_data(planning_id, lambda d: set_masque(d, key, masque))
    if data is None:
        return _error("Planning introuvable", 404)
    if not ok:
        return _error("Article inconnu", 404)
    return await _liste_html(planning_id, data)


@router.post("/api/courses/{planning_id}/placard")
async def courses_placard(planning_id: int, request: Request):
    """Range un article au placard (« Toujours à la maison ») ou l'en retire :
    {"key": "...", "present": true|false}. Le placard vaut pour tout le foyer."""
    body = await _body(request)
    key = str(body.get("key", ""))
    present = bool(body.get("present", True))
    planning = await db.get_planning_with_recipes(planning_id)
    if not planning:
        return _error("Planning introuvable", 404)
    data = json.loads(planning["data_json"])
    nom = recipe_item_name(data, key)
    if nom is None:
        return _error("Article inconnu", 404)
    await set_placard(nom, present)
    return await _liste_html(planning_id, data)
