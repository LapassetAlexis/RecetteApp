"""Routes des recettes : liste, fiche, édition (enrichissement), ajout,
notation, analyse d'URL ou de texte et enrichissement par lot."""

import json
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from app.config import BASE_OPTIONS, REPAS_OPTIONS, TAG_OPTIONS
from app.deps import BASE_SERVINGS, RATED_STATUS, db, llm, notion, templates
from app.services.planning import _json_or_none
from app.services.recettes import (
    _analysis_response,
    _cached_ingredients,
    _detail_base_servings,
    _detail_nutrition,
    _enrich_all_events,
    _enrich_many,
    _form_options,
    _planning_recipes,
    _recipe_stats,
)
from app.services.shopping import _ingredients_to_text
from app.text_utils import normalize_title_case, parse_ingredient_line, split_instructions

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

    # Stats pour les filtres (type / base / état / tag)
    stats = _recipe_stats(recettes, ings_by_id, duree_by_id)

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
            },
        )
    except Exception:
        logger.exception("Erreur détail recette")
        return HTMLResponse("Erreur interne lors du chargement de la recette.", status_code=500)


@router.get("/recette/{page_id}/enrichir", response_class=HTMLResponse)
async def enrichir_page(request: Request, page_id: str, url: str = ""):
    """Étape 1 : re-fetch la source et pré-remplit un formulaire éditable
    (ingrédients structurés + instructions nettoyées) à valider.
    Si la recette n'a pas d'URL, on peut en fournir une via ?url= pour extraire."""
    recette = await notion.get_recipe(page_id)
    if not recette:
        return HTMLResponse("Recette non trouvée", status_code=404)

    ingredients: list[dict] = []
    instructions_text = ""
    image_url = ""
    nutrition_src: dict = {}
    duree_minutes = 0

    # URL effective : celle de la recette, sinon celle fournie en paramètre.
    source_url = recette.get("url") or url.strip()
    needs_url = not recette.get("url")

    # « Enrichir » = re-fetch la SOURCE en priorité (données d'origine), pas le
    # cache qui peut contenir d'anciennes extractions inexactes.
    if source_url:
        try:
            info = await llm.extract_recipe_from_url(source_url)
            ingredients = [i for i in (parse_ingredient_line(x) for x in info.get("ingredients", [])) if i and i["nom"]]
            instructions_text = info.get("instructions", "")
            image_url = info.get("image_url", "")
            nutrition_src = info.get("nutrition") or {}
            duree_minutes = info.get("duree_minutes") or 0
        except Exception as e:
            logger.warning(f"Re-extraction enrichir {page_id}: {e}")

    # Repli sur le cache / les blocks Notion si la source n'a rien donné
    if not ingredients:
        cached = await db.get_enriched(page_id)
        if cached and cached.get("ingredients"):
            try:
                ingredients = json.loads(cached["ingredients"])
            except (json.JSONDecodeError, TypeError):
                ingredients = []
    if not instructions_text:
        try:
            instructions_text = "\n".join(await notion.get_recipe_instructions(page_id))
        except Exception:
            instructions_text = ""

    # Base de portions à pré-remplir : cache en priorité, sinon Notion, défaut 4.
    base_servings = recette.get("base_servings") or BASE_SERVINGS
    cached_bs = await db.get_enriched(page_id)
    if cached_bs and cached_bs.get("base_servings"):
        base_servings = cached_bs["base_servings"]

    return templates.TemplateResponse(
        request,
        "enrichir.html",
        {
            "recette": recette,
            "ingredients": ingredients,
            "steps": split_instructions(instructions_text),
            "image_url": image_url,
            "nutrition_json": json.dumps(nutrition_src),
            "duree_minutes": duree_minutes,
            "base_servings": base_servings,
            **_form_options(),
            "source_url": source_url,
            "needs_url": needs_url,
        },
    )


@router.post("/recette/{page_id}/enrichir")
async def enrichir_submit(
    request: Request,
    page_id: str,
    nom: str = Form(""),
    repas: list[str] = Form([]),
    tags: list[str] = Form([]),
    base: list[str] = Form([]),
    nature: str = Form("Recette"),
    ingredients_text: str = Form(""),
    steps: list[str] = Form([]),
    image_url: str = Form(""),
    nutrition_json: str = Form(""),
    duree_minutes: int = Form(0),
    base_servings: int = Form(4),
    source_url: str = Form(""),
):
    """Étape 2 : applique les changements validés à la recette Notion + cache."""
    recette = await notion.get_recipe(page_id)
    if not recette:
        return RedirectResponse(url="/recettes", status_code=303)

    # Renommage libre : on prend le nom soumis s'il y en a un, sinon le nom
    # actuel. On normalise la casse d'un titre crié en MAJUSCULES (ex. import),
    # mais on respecte une casse choisie par l'utilisateur (renommage libre).
    nom_saisi = nom.strip()
    nom_source = nom_saisi or recette["nom"]
    nom_norm = normalize_title_case(nom_source) if nom_source.isupper() else nom_source

    # URL source : si la recette n'en avait pas (ou différente), on l'enregistre.
    source_url = source_url.strip()

    structured = [
        i for i in (parse_ingredient_line(line) for line in ingredients_text.split("\n")) if i and i["nom"]
    ]
    instructions_text = "\n".join(s.strip() for s in steps if s.strip())
    # Nutrition exacte de la source si fournie et lisible (sinon vide → estimée
    # à l'affichage)
    nutrition = nutrition_json if _json_or_none(nutrition_json) else ""
    # On isole CHAQUE écriture Notion pour remonter précisément ce qui a échoué
    # (au lieu d'avaler tout dans un seul try/except et de rediriger en « succès »).
    echecs: list[str] = []          # étapes Notion ratées (label FR)
    cache_local_ok = False          # le cache local (ingrédients) a-t-il été sauvé ?

    async def _etape(label: str, coro) -> None:
        """Exécute une écriture Notion en isolant son échec éventuel."""
        try:
            await coro
        except Exception:
            logger.exception(f"Erreur validation enrichissement : {label}")
            echecs.append(label)

    if nom_norm != recette["nom"]:
        await _etape("titre", notion.update_recipe_title(page_id, nom_norm))
    if source_url and source_url != (recette.get("url") or ""):
        await _etape("URL source", notion.update_recipe_url(page_id, source_url))
    # Base de portions : bornée à >= 1 (une valeur <= 0 retomberait sur le défaut).
    bs = base_servings if base_servings and base_servings > 0 else 4
    if structured:
        # Cache local d'abord (distinct de Notion) : on note s'il a réussi.
        try:
            await db.save_enriched(page_id, nom_norm, ingredients=json.dumps(structured),
                                   cuisson_minutes=duree_minutes or 0, nutrition=nutrition,
                                   base_servings=bs)
            cache_local_ok = True
        except Exception:
            logger.exception("Erreur validation enrichissement : cache local")
        await _etape("ingrédients", notion.update_ingredients(page_id, _ingredients_to_text(structured)))
    await _etape("portions", notion.update_portions(page_id, bs))
    if instructions_text:
        await _etape("instructions", notion.rewrite_recipe_body(page_id, instructions_text))
    if image_url:
        await _etape("image", notion.update_image(page_id, image_url))
    await _etape("type/tags/base", notion.update_recipe_meta(
        page_id, repas=repas, tags=tags or None, nature=nature, base=base))

    if echecs:
        # Au moins une écriture Notion a échoué : on NE redirige PAS en succès.
        # On re-affiche le formulaire en conservant toute la saisie + un message
        # d'erreur précis (étapes ratées et sort du cache local).
        msg = "Échec d'enregistrement dans Notion pour : " + ", ".join(echecs) + "."
        if structured:
            msg += (" Le cache local des ingrédients a bien été enregistré."
                    if cache_local_ok else
                    " Le cache local des ingrédients n'a pas non plus pu être enregistré.")
        msg += " Rien n'a été validé : corrige puis re-valide."
        # On reflète la saisie dans la recette pour re-cocher type/tags/nom.
        recette["nom"] = nom_norm
        recette["repas"] = repas or []
        recette["tags"] = tags or []
        recette["base"] = base or []
        recette["nature"] = nature or "Recette"
        return templates.TemplateResponse(
            request,
            "enrichir.html",
            {
                "recette": recette,
                "ingredients": structured,
                "steps": [s.strip() for s in steps if s.strip()],
                "image_url": image_url,
                "nutrition_json": nutrition_json,
                "duree_minutes": duree_minutes,
                "base_servings": bs,
                **_form_options(),
                "source_url": source_url,
                "needs_url": not recette.get("url"),
                "error": msg,
            },
        )

    return RedirectResponse(url=f"/recette/{page_id}", status_code=303)


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
        {**_form_options(), "success": None, "error": None},
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
    """Ajoute une recette depuis une URL ou manuellement."""
    instructions_manual = "\n".join(s.strip() for s in steps if s.strip())
    error = None
    success = None

    try:
        if url and not nom:
            # Extraction automatique via LLM
            extracted = await llm.extract_recipe_from_url(url)
            nom = extracted.get("nom", "")
            if not repas and extracted.get("type_repas"):
                repas = [extracted["type_repas"]]
            if not tags:
                tags = extracted.get("tags", [])
            if not base:
                base = extracted.get("base", [])
        elif not nom:
            error = "Il faut au moins un nom ou une URL."
        else:
            # Si que URL sans extraction
            pass

        bs = base_servings if base_servings and base_servings > 0 else 4
        if not error:
            result = await notion.create_recipe(
                nom=nom,
                url=url,
                repas=repas,
                tags=tags,
                moment=moment,
                nature=nature,
                base=base,
                base_servings=bs,
            )
            page_id = result.get("id", "")
            recette_url = result.get("url", "")
            logger.info(f"Recette ajoutée: {nom} → {recette_url}")

            # Sauvegarder les ingrédients, instructions et image : chaque écriture
            # est isolée pour remonter précisément ce qui n'a pas pu être enregistré
            # (au lieu d'avaler l'échec et d'annoncer un « succès » trompeur).
            echecs: list[str] = []

            async def _etape(label: str, coro) -> None:
                try:
                    await coro
                except Exception as e:
                    logger.warning(f"Impossible de sauvegarder {label}: {e}")
                    echecs.append(label)

            if page_id and ingredients_manual:
                await _etape("les ingrédients", notion.update_ingredients(page_id, ingredients_manual))
            if page_id and instructions_manual:
                await _etape("les instructions", notion.append_instructions(page_id, instructions_manual))
            if page_id and image_url:
                await _etape("l'image", notion.update_image(page_id, image_url))
            if page_id and ingredients_manual:
                # Cache local : ingrédients STRUCTURÉS (pour scaling + nutrition)
                ings_list = [
                    i for i in (parse_ingredient_line(line) for line in ingredients_manual.split("\n"))
                    if i and i["nom"]
                ]
                await _etape("le cache local des ingrédients", db.save_enriched(
                    notion_id=page_id, recipe_name=nom, ingredients=json.dumps(ings_list),
                    base_servings=bs,
                ))

            if echecs:
                # Échec partiel : la page a bien été créée mais des infos manquent.
                # On n'affiche PAS un « succès » nu.
                error = (f"Recette « {nom} » créée dans Notion, mais impossible d'enregistrer : "
                         + ", ".join(echecs) + ". Ré-essaie depuis la page « Enrichir ».")
            else:
                success = f"Recette « {nom} » ajoutée avec succès !"

    except Exception as e:
        logger.exception("Erreur ajout recette")
        error = f"Erreur : {str(e)}"

    return templates.TemplateResponse(
        request,
        "ajouter.html",
        {"success": success, "error": error, **_form_options()},
    )


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
