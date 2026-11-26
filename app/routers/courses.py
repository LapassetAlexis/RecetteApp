"""Routes de la liste de courses : génération, coches, articles manuels,
placard et partage public (le jeton donne le droit de cocher, rien d'autre)."""

import json
import logging
import time
from collections import deque

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.deps import db, templates
from app.http import ERREUR_INTERNE, erreur, lire_json
from app.services import store
from app.services.planning import day_labels, per_day_list
from app.services.shopping import collect_shopping, finalize_items, purge_orphans, repas_libres
from app.services.shopping_list import (
    add_extra,
    apply_check,
    get_placard,
    load_shopping_view,
    mutate_planning_data,
    recipe_item_name,
    remove_extra,
    reset_checks,
    set_extra_rayon,
    set_masque,
    set_placard,
    share_action,
)
from app.text_utils import merge_ingredients

logger = logging.getLogger(__name__)
router = APIRouter()


def _error(message: str, status: int) -> JSONResponse:
    return erreur(message, status)


async def _body(request: Request, code: str | None = None) -> dict:
    """Corps JSON de la requête ({} s'il est absent) ; illisible → 400, plus
    de 64 Ko → 413 (cf. `app.http.lire_json`)."""
    return await lire_json(request, code)


async def _liste_html(planning_id: int, data: dict) -> dict:
    """Réponse des actions de la page planning : la liste re-rendue côté serveur
    (même gabarit que l'affichage initial) et le nombre d'articles visibles."""
    shop = await load_shopping_view(data)
    html = templates.get_template("partials/_shopping_list.html").render(
        shop=shop, planning={"id": planning_id},
    )
    return {"success": True, "html": html, "nb": shop["nb_total"],
            "nb_placard": shop["nb_placard"]}


async def _etat(data: dict) -> dict:
    """État des coches : clés cochées et empreinte des lignes visibles."""
    shop = await load_shopping_view(data)
    return {"checked": shop["checked"], "version": shop["version"]}


def _revoque() -> JSONResponse:
    """Jeton inconnu ou révoqué : 404 que la page publique reconnaît."""
    return JSONResponse({"error": "Ce lien n'est plus valide", "code": "revoque"}, status_code=404)


# Limite simple des coches publiques, en mémoire (un seul processus uvicorn).
CHECK_LIMIT = 120           # requêtes par fenêtre et par jeton
CHECK_WINDOW = 60.0         # secondes
_check_hits: dict[str, deque] = {}


def _check_autorise(token: str, now: float | None = None) -> bool:
    """Fenêtre glissante : True si `token` a fait moins de CHECK_LIMIT coches
    dans les CHECK_WINDOW dernières secondes (et compte celle-ci)."""
    now = time.monotonic() if now is None else now
    hits = _check_hits.setdefault(token, deque())
    while hits and now - hits[0] >= CHECK_WINDOW:
        hits.popleft()
    if len(hits) >= CHECK_LIMIT:
        return False
    hits.append(now)
    # Jetons inactifs retirés : la table ne grossit pas sans fin.
    if len(_check_hits) > 1000:
        for t in [t for t, h in _check_hits.items() if not h or now - h[-1] >= CHECK_WINDOW]:
            _check_hits.pop(t, None)
    return True


@router.post("/generate-shopping/{planning_id}")
async def generate_shopping(planning_id: int, request: Request):
    """Génère la liste de courses pour un planning existant (sous le verrou
    du planning : une coche simultanée n'est ni perdue ni réécrite)."""

    async def fn(planning: dict, planning_data: dict):
        plats = planning_data.get("plats", [])
        if not plats:
            return store.NoWrite({"error": "Aucun plat dans ce planning"})
        # Ingrédients ANCRÉS sur le cache DB (pas d'extraction LLM à l'aveugle),
        # mis à l'échelle selon le nb de personnes de chaque repas, puis fusion
        # déterministe (dédup + addition par nom+unité, union des recettes),
        # rayon et clé de chaque article.
        collected, non_enrichis = await collect_shopping(
            plats, per_day_list(planning, planning_data), planning["nb_personnes"],
        )
        liste_courses = finalize_items(merge_ingredients(collected))
        # NB : cette liste agrégée n'est PAS écrite dans Notion par recette.
        planning_data["liste_courses"] = liste_courses
        # Nouveaux items : on repart sans coche (sauf articles manuels, qui
        # survivent à la régénération), masques orphelins retirés. Le
        # share_token éventuel est conservé tel quel.
        reset_checks(planning_data)
        purge_orphans(planning_data)
        return {
            "success": True,
            "liste_courses": liste_courses,
            "non_enrichis": sorted(set(non_enrichis)),
        }

    try:
        data, res = await store.update_planning(planning_id, fn)
        if data is None:
            return _error("Planning introuvable", 404)
        if isinstance(res, dict) and res.get("error"):
            return _error(res["error"], 400)
        return res
    except Exception:
        logger.exception("Erreur génération liste courses")
        return _error(ERREUR_INTERNE, 500)


@router.post("/api/shopping-check/{planning_id}")
async def shopping_check(planning_id: int, request: Request):
    """Coche/décoche un item de la liste de courses côté serveur.

    Body JSON : {"item": "<clé d'article>", "checked": true|false}. Un nom seul
    (ancien client) est encore accepté. L'état est persisté dans
    planning_data["courses_checked"] (liste de clés)."""
    body = await _body(request)
    item = str(body.get("item", "")).strip().lower()
    checked = bool(body.get("checked"))
    if not item:
        return _error("Item manquant", 400)
    try:
        data, ok = await mutate_planning_data(
            planning_id, lambda d: apply_check(d, item, checked),
        )
    except Exception:
        logger.exception("Erreur mise à jour case liste de courses")
        return _error(ERREUR_INTERNE, 500)
    if data is None:
        return _error("Planning introuvable", 404)
    # Clé inconnue (article disparu, coche rejouée hors-ligne) : rien à faire,
    # mais ce n'est pas une erreur à rejouer.
    return {"success": True, "ignored": not ok}


@router.post("/planning/{planning_id}/partager")
async def partager_liste(planning_id: int, request: Request):
    """Lien de partage public de la liste (D8 : révocable).

    Corps facultatif {"action": "revoquer" | "regenerer"}. Sans action : le
    lien existant, sinon un nouveau. Renvoie {"url": "/courses/<jeton>"}, ou
    {"url": null} après révocation."""
    action = str((await _body(request)).get("action") or "")
    if action not in ("", "revoquer", "regenerer"):
        return _error("Action inconnue", 400)

    def fn(_planning: dict, planning_data: dict):
        avant = planning_data.get("share_token")
        token = share_action(planning_data, action)
        return token if token != avant else store.NoWrite(token)

    try:
        data, token = await store.update_planning(planning_id, fn)
        if data is None:
            return _error("Planning introuvable", 404)
        return {"url": f"/courses/{token}" if token else None}
    except Exception:
        logger.exception("Erreur lien de partage")
        return _error(ERREUR_INTERNE, 500)


# ── Page publique : lecture + coches uniquement ─────────────────────────

@router.get("/courses/{token}", response_class=HTMLResponse)
async def courses_public(request: Request, token: str):
    """Liste de courses publique : articles à prendre groupés par rayon, les
    articles cochés repliés en bas sous « Déjà pris ». Accès par token
    (exempté de l'authentification), qui ne permet que de cocher."""
    planning = await db.get_planning_by_share_token(token)
    if not planning:
        # Lien révoqué ou inconnu : la page vide sa file hors-ligne et le
        # service worker oublie ce lien (courses-public.js, sw.js).
        return templates.TemplateResponse(
            request, "courses_public.html", {"revoque": True}, status_code=404,
        )

    data = json.loads(planning["data_json"])
    return templates.TemplateResponse(
        request,
        "courses_public.html",
        {
            "shop": await load_shopping_view(data),
            "repas_libres": repas_libres(
                data.get("plats", []), day_labels(planning.get("week_start", "")),
            ),
        },
    )


@router.get("/courses/{token}/etat")
async def courses_public_etat(token: str):
    """État des coches pour le rafraîchissement de la page publique.

    `version` change quand les lignes de la liste changent : la page se recharge."""
    planning = await db.get_planning_by_share_token(token)
    if not planning:
        return _revoque()
    return await _etat(json.loads(planning["data_json"]))


@router.post("/courses/{token}/check")
async def courses_public_check(token: str, request: Request):
    """Coche / décoche depuis le lien public. Même corps que /api/shopping-check,
    mais seule une clé d'article existante est acceptée. Au plus
    CHECK_LIMIT requêtes par minute et par jeton (429 au-delà)."""
    if not _check_autorise(token):
        return JSONResponse({"error": "Trop de requêtes, réessaie dans un instant",
                             "code": "limite"}, status_code=429)
    planning = await db.get_planning_by_share_token(token)
    if not planning:
        return _revoque()
    body = await _body(request, code="corps")
    item = str(body.get("item", ""))
    checked = bool(body.get("checked"))
    data, ok = await mutate_planning_data(
        planning["id"], lambda d: apply_check(d, item, checked, strict=True),
    )
    if data is None:
        return _revoque()
    if not ok:
        return JSONResponse({"error": "Article inconnu", "code": "inconnu"}, status_code=400)
    return {"success": True}


@router.get("/api/courses/{planning_id}/etat")
async def courses_etat(planning_id: int):
    """État des coches pour le rafraîchissement de la page planning : même
    réponse que /courses/{token}/etat."""
    planning = await db.get_planning_with_recipes(planning_id)
    if not planning:
        return _error("Planning introuvable", 404)
    return await _etat(store.load(planning.get("data_json")))


# ── Articles manuels, masques et placard (page planning, authentifiée) ──

@router.post("/api/courses/{planning_id}/extra")
async def courses_extra_add(planning_id: int, request: Request):
    """Ajoute un article à la main : {"texte": "2 l lait"}."""
    texte = str((await _body(request)).get("texte", ""))
    data, item = await mutate_planning_data(planning_id, lambda d: add_extra(d, texte))
    if data is None:
        return _error("Planning introuvable", 404)
    if item is None:
        return _error("Article refusé : nom vide ou sans lettre, ou liste pleine", 400)
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


@router.post("/api/courses/{planning_id}/extra/{key}/rayon")
async def courses_extra_rayon(planning_id: int, key: str, request: Request):
    """Range un article manuel dans un autre rayon : {"rayon": "Boissons"}."""
    rayon = str((await _body(request)).get("rayon", ""))
    data, ok = await mutate_planning_data(
        planning_id, lambda d: set_extra_rayon(d, key, rayon),
    )
    if data is None:
        return _error("Planning introuvable", 404)
    if not ok:
        return _error("Article ou rayon inconnu", 400)
    return await _liste_html(planning_id, data)


# ── Placard du foyer : écran de gestion ─────────────────────────────────

@router.get("/api/placard")
async def placard_liste():
    """Contenu du placard (« Toujours à la maison »), trié par nom."""
    return {"placard": await get_placard()}


@router.delete("/api/placard/{nom}")
async def placard_retirer(nom: str, planning: int | None = None):
    """Retire un article du placard. Avec `?planning=<id>`, renvoie aussi la
    liste de ce planning re-rendue (l'article y redevient visible)."""
    if not await set_placard(nom, False):
        return _error("Article absent du placard", 404)
    res = {"success": True, "placard": await get_placard()}
    if planning is not None:
        p = await db.get_planning_with_recipes(planning)
        if p:
            res.update(await _liste_html(planning, store.load(p.get("data_json"))))
    return res
