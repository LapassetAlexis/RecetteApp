"""Routes des recettes : liste, fiche, édition, ajout, ajout au planning,
notation, analyse d'URL ou de texte et enrichissement par lot."""

import json
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from app.config import BASE_OPTIONS, REPAS_OPTIONS, TAG_OPTIONS
from app.deps import RATED_STATUS, db, llm, notion, templates
from app.http import (
    ERREUR_INTERNE,
    ERREUR_LLM,
    ERREUR_NOTION,
    depuis_service,
    erreur,
    erreur_exception,
    lire_json,
)
from app.services.enrichissement import enrich_all_events, enrich_many, planning_recipes
from app.services.recette_edition import ajouter, analysis_response, enrichir_context, enrichir_enregistrer
from app.services.recette_planifier import draft_choices_safe, planifier
from app.services.recettes import (
    attach_usage,
    cached_ingredients,
    detail_base_servings,
    detail_nutrition,
    form_options,
    recipe_stats,
    recipe_usage,
    usage_label,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/recettes", response_class=HTMLResponse)
async def liste_recettes(request: Request):
    """Liste toutes les recettes de la base Notion."""
    try:
        recettes = await notion.get_all_recipes()
    except Exception as e:
        logger.error(f"Erreur Notion: {e}")
        recettes = []

    # Ingrédients + durée en cache (recherche par ingrédient, badge temps)
    try:
        ings_by_id = await db.get_all_enriched_ingredients()
        duree_by_id = await db.get_all_enriched_durations()
    except Exception as e:
        logger.warning(f"Lecture cache recettes échouée: {e}")
        ings_by_id, duree_by_id = {}, {}

    # Stats pour les filtres (type / base / état / tag) + usage (tri, badge)
    stats = recipe_stats(recettes, ings_by_id, duree_by_id)
    attach_usage(recettes, await recipe_usage())

    return templates.TemplateResponse(
        request,
        "recettes.html",
        {
            "recettes": recettes,
            **stats,
            "repas_options": REPAS_OPTIONS,
            "tag_options": TAG_OPTIONS,
            "base_options": BASE_OPTIONS,
        },
    )


@router.get("/recette/{page_id}", response_class=HTMLResponse)
async def detail_recette(request: Request, page_id: str):
    """Fiche détail : ingrédients (Cooklang, portions ajustables) + instructions."""
    try:
        recette = await notion.get_recipe(page_id)  # 1 appel, pas toute la base
        if not recette:
            return HTMLResponse("Recette non trouvée", status_code=404)

        # Ingrédients structurés depuis le cache local
        cached = await db.get_enriched(page_id)
        ingredients = cached_ingredients(cached)

        # Base de portions de CETTE recette : priorité au cache, repli sur la
        # valeur Notion (Portions) lue par _parse_page, défaut 4.
        base_servings = detail_base_servings(cached, recette)

        # Instructions depuis les blocks Notion (best-effort)
        try:
            instructions = await notion.get_recipe_instructions(page_id)
        except Exception as e:
            logger.warning(f"Instructions illisibles pour {page_id}: {e}")
            instructions = []

        duree = cached.get("cuisson_minutes") if cached else 0
        usage = (await recipe_usage()).get(page_id)
        return templates.TemplateResponse(
            request,
            "recette_detail.html",
            {
                "recette": recette,
                "ingredients": ingredients,
                "base_servings": base_servings,
                "instructions": instructions,
                "nutrition": detail_nutrition(cached, ingredients, base_servings),
                "duree": duree or 0,
                "usage_label": usage_label(usage),
                "drafts": await draft_choices_safe(),
            },
        )
    except Exception:
        logger.exception("Erreur détail recette")
        return HTMLResponse("Erreur interne lors du chargement de la recette.", status_code=500)


@router.get("/recette/{page_id}/enrichir", response_class=HTMLResponse)
async def enrichir_page(request: Request, page_id: str, url: str = "", refetch: int = 0):
    """Étape 1 : formulaire d'édition pré-rempli avec les données enregistrées
    (cache local + Notion), sans appel à la source. `?refetch=1` ou `?url=`
    (« Extraire ») ré-extrait depuis la source ; une recette sans ingrédient en
    cache mais avec une URL est extraite automatiquement."""
    recette = await notion.get_recipe(page_id)
    if not recette:
        return HTMLResponse("Recette non trouvée", status_code=404)
    ctx = await enrichir_context(recette, page_id, url=url, refetch=bool(refetch))
    return templates.TemplateResponse(request, "enrichir.html", ctx)


@router.post("/recette/{page_id}/enrichir")
async def enrichir_submit(
    request: Request,
    page_id: str,
    nom: str = Form(""),
    repas: list[str] = Form([]),
    tags: list[str] = Form([]),
    base: list[str] = Form([]),
    nature: str = Form("Recette"),
    moment: str | None = Form(None),
    etat: str | None = Form(None),
    ingredients_text: str = Form(""),
    steps: list[str] = Form([]),
    image_url: str = Form(""),
    nutrition_json: str = Form(""),
    duree_minutes: int = Form(0),
    base_servings: int = Form(4),
    source_url: str = Form(""),
):
    """Étape 2 : applique les changements validés à la recette Notion + cache.
    En cas d'échec Notion, le formulaire est ré-affiché avec toute la saisie."""
    recette = await notion.get_recipe(page_id)
    if not recette:
        return RedirectResponse(url="/recettes", status_code=303)
    erreur = await enrichir_enregistrer(page_id, recette, {
        "nom": nom, "repas": repas, "tags": tags, "base": base, "nature": nature,
        "moment": moment, "etat": etat, "ingredients_text": ingredients_text, "steps": steps,
        "image_url": image_url, "nutrition_json": nutrition_json, "duree_minutes": duree_minutes,
        "base_servings": base_servings, "source_url": source_url,
    })
    if erreur:
        return templates.TemplateResponse(request, "enrichir.html", erreur)
    return RedirectResponse(url=f"/recette/{page_id}", status_code=303)


@router.post("/api/recette/{page_id}/planifier")
async def api_planifier(page_id: str, request: Request):
    """Ajoute la recette à un brouillon depuis sa fiche.

    JSON : {planning_id (id d'un brouillon, ou "new"), jour 1-7, moment
    midi|soir, date AAAA-MM-JJ (nouveau brouillon), persons (optionnel)}.
    Le plat du créneau est créé ou remplacé, la liste de courses régénérée."""
    body = await lire_json(request)
    try:
        return depuis_service(await planifier(page_id, body))
    except Exception as e:
        logger.exception("Erreur ajout au planning")
        return erreur_exception(e)


@router.post("/recette/{page_id}/supprimer")
async def supprimer_recette(page_id: str):
    """Supprime une recette : archive la page Notion + purge le cache local.
    Renvoie du JSON (appelée en fetch depuis la fiche détail)."""
    try:
        await notion.archive_recipe(page_id)
    except Exception:
        logger.exception("Erreur suppression recette (Notion)")
        return erreur("Impossible de supprimer la recette : Notion est injoignable.", 502)
    try:
        await db.delete_enriched(page_id)
    except Exception:
        logger.exception("Erreur suppression recette (cache)")
        return erreur(ERREUR_INTERNE, 500)
    return {"success": True}


@router.get("/ajouter", response_class=HTMLResponse)
async def ajouter_page(request: Request):
    """Page pour ajouter une recette depuis une URL."""
    return templates.TemplateResponse(
        request,
        "ajouter.html",
        {**form_options(), "success": None, "error": None, "form": None},
    )


@router.post("/ajouter-recette")
async def ajouter_recette(
    request: Request,
    url: str = Form(""),
    nom: str = Form(""),
    repas: list[str] = Form([]),
    tags: list[str] = Form([]),
    base: list[str] = Form([]),
    nature: str = Form("Recette"),
    moment: str = Form(""),
    base_servings: int = Form(4),
    ingredients_manual: str = Form(""),
    steps: list[str] = Form([]),
    image_url: str = Form(""),
):
    """Ajoute une recette depuis une URL ou manuellement. En cas d'erreur, le
    formulaire est ré-affiché à l'étape 2 avec toutes les valeurs saisies."""
    ctx = await ajouter({
        "url": url, "nom": nom, "repas": repas, "tags": tags, "base": base, "nature": nature,
        "moment": moment, "base_servings": base_servings, "ingredients_manual": ingredients_manual,
        "steps": steps, "image_url": image_url,
    })
    return templates.TemplateResponse(request, "ajouter.html", ctx)


@router.post("/api/rate/{page_id}")
async def api_rate(page_id: str, request: Request):
    """Note une recette (⭐ à ⭐⭐⭐⭐⭐)."""
    data = await lire_json(request)
    note = data.get("note", "")
    # "" = retirer la note ; sinon une des 5 valeurs étoilées
    if note != "" and note not in ("⭐", "⭐⭐", "⭐⭐⭐", "⭐⭐⭐⭐", "⭐⭐⭐⭐⭐"):
        return erreur("Note invalide")
    try:
        await notion.update_rating(page_id, note)
        # Noter une recette = elle a été testée → on la sort de « À essayer ».
        warning = ""
        if note:
            try:
                await notion.update_status(page_id, RATED_STATUS)
            except Exception as e:
                logger.warning(f"Passage à « {RATED_STATUS} » échoué pour {page_id}: {e}")
                warning = (f"Note enregistrée, mais l'état n'a pas pu passer à "
                           f"« {RATED_STATUS} » (option Notion introuvable ?).")
        return {"success": True, "warning": warning}
    except Exception:
        logger.exception("Erreur notation")
        return erreur(ERREUR_NOTION, 502)


@router.post("/api/analyze-url")
async def api_analyze_url(request: Request):
    """Analyse une URL et retourne les infos extraites."""
    data = await lire_json(request)
    url = str(data.get("url") or "").strip()
    if not url:
        return erreur("URL manquante")
    try:
        # Extraction unifiée : titre, type, tags, ingrédients, instructions,
        # image — depuis la page elle-même (JSON-LD si dispo, sinon LLM).
        # Plus de second appel "à l'aveugle" qui hallucinait les ingrédients.
        info = await llm.extract_recipe_from_url(url)
        return analysis_response(info, info.get("image_url", ""), info.get("source", ""))

    except Exception:
        logger.exception("Erreur analyse URL")
        return erreur("Analyse de la page impossible : vérifie l'adresse ou réessaie plus tard.", 502)


@router.post("/api/analyze-text")
async def api_analyze_text(request: Request):
    """Structure une recette collée en texte libre (Gemini, note perso...)."""
    data = await lire_json(request)
    text = data.get("text", "")
    if not isinstance(text, str) or not text.strip():
        return erreur("Texte manquant")
    try:
        info = await llm.extract_recipe_from_text(text)
        return analysis_response(info, "", info.get("source", "llm-text"))

    except Exception:
        logger.exception("Erreur analyse texte")
        return erreur(ERREUR_LLM, 502)


@router.post("/api/enrich-week/{planning_id}")
async def api_enrich_week(planning_id: int):
    """Relance l'enrichissement de toutes les recettes d'un planning (plats +
    accompagnements), sans toucher au reste du catalogue."""
    planning = await db.get_planning_with_recipes(planning_id)
    if not planning:
        return erreur("Planning introuvable", 404)
    try:
        plats = json.loads(planning["data_json"]).get("plats", [])
        recettes = planning_recipes(plats)
        counts = await enrich_many(recettes)
        return {"success": True, "total": len(recettes), **counts}
    except Exception as e:
        logger.exception("Erreur enrichissement semaine")
        return erreur_exception(e, ERREUR_NOTION)


async def _events_sans_detail():
    """Événements de `enrich_all_events`, l'événement `error` ramené à un
    message générique (le détail de l'exception est déjà dans les logs)."""
    async for ev in enrich_all_events():
        if ev.startswith("event: error"):
            ev = f"event: error\ndata: {json.dumps({'error': ERREUR_INTERNE}, ensure_ascii=False)}\n\n"
        yield ev


@router.post("/api/enrich-all/stream")
async def api_enrich_all_stream(request: Request):
    """Enrichit toutes les recettes en streamant la progression (SSE lu par
    `fetch` en flux dans `js/recettes-liste.js`).

    L'action écrit dans Notion : POST seulement, et un formulaire HTML
    (`Accept: text/html`) est refusé (405) ; seul un client de flux la lance."""
    if "text/html" in request.headers.get("accept", ""):
        return erreur("Action à lancer depuis la page Recettes.", 405)
    return StreamingResponse(_events_sans_detail(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })
