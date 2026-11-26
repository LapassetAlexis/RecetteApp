"""Routes des recettes : liste, fiche, édition, ajout, ajout au planning,
notation, analyse d'URL ou de texte et enrichissement par lot."""

import json
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from app.config import BASE_OPTIONS, REPAS_OPTIONS, TAG_OPTIONS
from app.deps import RATED_STATUS, db, llm, notion, templates
from app.services.recettes import (
    _ajouter,
    _analysis_response,
    _attach_usage,
    _cached_ingredients,
    _detail_base_servings,
    _detail_nutrition,
    _draft_choices_safe,
    _enrich_all_events,
    _enrich_many,
    _enrichir_context,
    _enrichir_enregistrer,
    _form_options,
    _planifier,
    _planning_recipes,
    _recipe_stats,
    _recipe_usage,
    _usage_label,
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
    stats = _recipe_stats(recettes, ings_by_id, duree_by_id)
    _attach_usage(recettes, await _recipe_usage())

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
        ingredients = _cached_ingredients(cached)

        # Base de portions de CETTE recette : priorité au cache, repli sur la
        # valeur Notion (Portions) lue par _parse_page, défaut 4.
        base_servings = _detail_base_servings(cached, recette)

        # Instructions depuis les blocks Notion (best-effort)
        try:
            instructions = await notion.get_recipe_instructions(page_id)
        except Exception as e:
            logger.warning(f"Instructions illisibles pour {page_id}: {e}")
            instructions = []

        duree = cached.get("cuisson_minutes") if cached else 0
        usage = (await _recipe_usage()).get(page_id)
        return templates.TemplateResponse(
            request,
            "recette_detail.html",
            {
                "recette": recette,
                "ingredients": ingredients,
                "base_servings": base_servings,
                "instructions": instructions,
                "nutrition": _detail_nutrition(cached, ingredients, base_servings),
                "duree": duree or 0,
                "usage_label": _usage_label(usage),
                "drafts": await _draft_choices_safe(),
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
    ctx = await _enrichir_context(recette, page_id, url=url, refetch=bool(refetch))
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
    erreur = await _enrichir_enregistrer(page_id, recette, {
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
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return {"error": "Corps JSON attendu"}
    try:
        return await _planifier(page_id, body)
    except Exception as e:
        logger.exception("Erreur ajout au planning")
        return {"error": str(e)}


@router.post("/recette/{page_id}/supprimer")
async def supprimer_recette(page_id: str):
    """Supprime une recette : archive la page Notion + purge le cache local.
    Renvoie du JSON (appelée en fetch depuis la fiche détail)."""
    try:
        await notion.archive_recipe(page_id)
        await db.delete_enriched(page_id)
    except Exception:
        logger.exception("Erreur suppression recette")
        return {"error": "Impossible de supprimer la recette."}
    return {"success": True}


@router.get("/ajouter", response_class=HTMLResponse)
async def ajouter_page(request: Request):
    """Page pour ajouter une recette depuis une URL."""
    return templates.TemplateResponse(
        request,
        "ajouter.html",
        {**_form_options(), "success": None, "error": None, "form": None},
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
    ctx = await _ajouter({
        "url": url, "nom": nom, "repas": repas, "tags": tags, "base": base, "nature": nature,
        "moment": moment, "base_servings": base_servings, "ingredients_manual": ingredients_manual,
        "steps": steps, "image_url": image_url,
    })
    return templates.TemplateResponse(request, "ajouter.html", ctx)


@router.post("/api/rate/{page_id}")
async def api_rate(page_id: str, request: Request):
    """Note une recette (⭐ à ⭐⭐⭐⭐⭐)."""
    try:
        data = await request.json()
        note = data.get("note", "")
        # "" = retirer la note ; sinon une des 5 valeurs étoilées
        if note != "" and note not in ("⭐", "⭐⭐", "⭐⭐⭐", "⭐⭐⭐⭐", "⭐⭐⭐⭐⭐"):
            return {"error": "Note invalide"}
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
    except Exception as e:
        logger.exception("Erreur notation")
        return {"error": str(e)}


@router.post("/api/analyze-url")
async def api_analyze_url(request: Request):
    """Analyse une URL et retourne les infos extraites."""
    try:
        data = await request.json()
        url = data.get("url", "")
        if not url:
            return {"error": "URL manquante"}

        # Extraction unifiée : titre, type, tags, ingrédients, instructions,
        # image — depuis la page elle-même (JSON-LD si dispo, sinon LLM).
        # Plus de second appel "à l'aveugle" qui hallucinait les ingrédients.
        info = await llm.extract_recipe_from_url(url)
        return _analysis_response(info, info.get("image_url", ""), info.get("source", ""))

    except Exception as e:
        logger.exception("Erreur analyse URL")
        return {"error": str(e)}


@router.post("/api/analyze-text")
async def api_analyze_text(request: Request):
    """Structure une recette collée en texte libre (Gemini, note perso...)."""
    try:
        data = await request.json()
        text = data.get("text", "")
        if not text or not text.strip():
            return {"error": "Texte manquant"}

        info = await llm.extract_recipe_from_text(text)
        return _analysis_response(info, "", info.get("source", "llm-text"))

    except Exception as e:
        logger.exception("Erreur analyse texte")
        return {"error": str(e)}


@router.post("/api/enrich-week/{planning_id}")
async def api_enrich_week(planning_id: int):
    """Relance l'enrichissement de toutes les recettes d'un planning (plats +
    accompagnements), sans toucher au reste du catalogue."""
    try:
        planning = await db.get_planning_with_recipes(planning_id)
        if not planning:
            return {"error": "Planning introuvable"}
        plats = json.loads(planning["data_json"]).get("plats", [])

        recettes = _planning_recipes(plats)
        counts = await _enrich_many(recettes)
        return {"success": True, "total": len(recettes), **counts}
    except Exception as e:
        logger.exception("Erreur enrichissement semaine")
        return {"error": str(e)}


@router.get("/api/enrich-all/stream")
async def api_enrich_all_stream():
    """Variante SSE : enrichit toutes les recettes en streamant la progression
    (évite le timeout d'une requête synchrone longue et donne un retour live)."""
    return StreamingResponse(_enrich_all_events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })
