"""Routes du planning : construction depuis la grille, affichage, cycle de vie
(dupliquer, valider, supprimer) et modifications des repas."""

import json
import logging
from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.categories import RAYON_ORDER, categorize, group_by_rayon
from app.config import BASE_OPTIONS, REPAS_OPTIONS
from app.deps import db, notion, templates
from app.services.nutrition_week import _week_nutrition
from app.services.planning import (
    MEAL_ACTIONS,
    _alternatives,
    _brique_ingredients,
    _builder_from_plats,
    _day_labels,
    _editable_planning,
    _group_week,
    _index_by_name,
    _json_or_none,
    _load_slot,
    _mark_non_enrichis,
    _meal_label,
    _merge_existing_data,
    _nb_jours,
    _nb_jours_from_cases,
    _parse_builder_meals,
    _parse_slots,
    _per_day_from_cases,
    _per_day_list,
    _place_brique,
    _plats_from_cases,
    _recipe_rows,
    _repas_libres,
    _resolve_side,
    _set_main,
    _week_label,
    apply_meals_batch,
)
from app.services.shopping import (
    _collect_shopping,
    _ingredients_to_text,
    _mutate_sides,
    _refresh_shopping_and_save,
)
from app.text_utils import merge_ingredients, parse_ingredient_line

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/construire")
async def construire(
    request: Request,
    week_start: str = Form(...),
    meals: str = Form(""),
    action: str = Form("valider"),
    draft_id: str = Form(""),
):
    """Construit un planning À LA MAIN depuis la grille du formulaire.

    Le champ caché `meals` (JSON) porte les cases de la semaine :
    [{jour 1-7, moment midi|soir, persons (0 = absent), group (midis),
      main {notion_id, nom, nature} | null,
      accompagnements [{notion_id, nom}, ...]}].
    Cases sans `main` ou avec persons <= 0 → ignorées. Les midis d'un même
    groupe partagent le repas du 1er midi rempli du groupe (même couleur =
    même plat). On sauvegarde un BROUILLON + sa liste de courses.

    `action` :
      - « valider » (défaut) → redirige vers la page planning (flux existant) ;
      - « brouillon » → reste en mode brouillon, redirige vers `/?draft=<id>`.
    `draft_id` (optionnel) : si fourni, on MET À JOUR ce planning au lieu d'en
    créer un nouveau. Les brouillons multiples coexistent (aucune purge). Une
    semaine validée rouverte (`/?planning=<id>`) reste validée, et « brouillon »
    renvoie alors vers `/?planning=<id>`.
    `data["nb_jours"]` = dernier jour de la période (dernier jour à convives)."""

    def _error_ctx(message: str) -> dict:
        # On repasse la grille saisie (builder) + le draft_id pour ne pas perdre
        # la saisie de l'utilisateur en cas d'erreur.
        return {
            "error": message,
            "week_start": week_start,
            "builder": _json_or_none(meals) or [],
            "draft_id": draft_id,
            "saved": False,
        }

    try:
        cases = _parse_builder_meals(meals)
        actives = [c for c in cases if c["persons"] > 0]

        # Catalogue Notion indexé par id (report des infos : base, nature, url…).
        recettes_all = await notion.get_all_recipes()
        by_id = {r["id"]: r for r in recettes_all}
        plats = _plats_from_cases(actives, by_id)

        if not plats:
            return templates.TemplateResponse(
                request, "index.html",
                _error_ctx("Aucun repas renseigné. Choisis au moins un plat principal."),
            )

        # Personnes de référence + per_day (repli d'affichage / anciens plannings).
        nb_personnes = max((c["persons"] for c in actives), default=4)
        per_day_list = _per_day_from_cases(cases, nb_personnes)
        per_day = ",".join(str(x) for x in per_day_list)
        off_meals = sorted(
            f"{c['jour']}:{c['moment']}" for c in cases if c["persons"] <= 0
        )

        # Liste de courses ANCRÉE sur le cache DB (mêmes règles que le reste de
        # l'app : pas d'extraction LLM à l'aveugle, mise à l'échelle PAR REPAS).
        collected, _ = await _collect_shopping(plats, per_day_list, nb_personnes)
        liste_courses = merge_ingredients(collected)
        for it in liste_courses:
            it["rayon"] = categorize(it.get("nom", ""))

        # Sauvegarde en BROUILLON (valide=0). Les brouillons multiples
        # coexistent : on ne purge PLUS les autres. On stocke aussi la grille
        # brute du formulaire (`builder`) pour pouvoir ré-éditer ce brouillon.
        data = {
            "plats": plats,
            "liste_courses": liste_courses,
            "per_day": per_day,
            "off_meals": off_meals,
            "builder": _json_or_none(meals) or [],
            "nb_jours": _nb_jours_from_cases(cases) or _nb_jours({"plats": plats}),
        }
        recipes = _recipe_rows(plats)

        # Mise à jour d'un planning existant (draft_id valide) plutôt que
        # création d'un nouveau : les autres clés de data_json sont gardées.
        existing = await _editable_planning(draft_id)
        target_id = existing["id"] if existing else 0
        if target_id:
            data = _merge_existing_data(existing, data)
            await db.update_planning(
                planning_id=target_id,
                data_json=json.dumps(data, ensure_ascii=False),
                recipes=recipes,
                week_start=week_start,
                nb_personnes=nb_personnes,
            )
            planning_id = target_id
        else:
            planning_id = await db.save_planning(
                week_start=week_start,
                saison="",
                nb_personnes=nb_personnes,
                ingredients_force="",
                data_json=json.dumps(data, ensure_ascii=False),
                recipes=recipes,
            )

        if action == "brouillon":
            # On RESTE dans le constructeur pré-rempli (semaine validée : elle
            # le reste, on la rouvre par ?planning=).
            param = "planning" if existing and existing.get("valide", 0) else "draft"
            return RedirectResponse(url=f"/?{param}={planning_id}&saved=1", status_code=303)
        return RedirectResponse(url=f"/planning/{planning_id}", status_code=303)

    except Exception as e:
        logger.exception("Erreur lors de la construction du planning")
        return templates.TemplateResponse(request, "index.html", _error_ctx(f"Erreur : {str(e)}"))


@router.get("/planning/{planning_id}", response_class=HTMLResponse)
async def voir_planning(request: Request, planning_id: int):
    """Affiche un planning existant."""
    planning = await db.get_planning_with_recipes(planning_id)
    if not planning:
        return RedirectResponse(url="/")

    data = json.loads(planning["data_json"])

    # Personnes par jour (anciens plannings : repli sur nb_personnes pour tous)
    per_day = _per_day_list(planning, data)

    liste_courses = data.get("liste_courses", [])
    plats = data.get("plats", [])

    # Marque les repas dont la recette n'a pas d'ingrédients en cache
    # (« recette pas terminée ») pour afficher un avertissement dans le planning.
    _mark_non_enrichis(plats, set(await db.get_all_enriched_ingredients()))

    # Libellés des 7 jours dérivés de la date du jour 1 (week_start).
    day_labels = _day_labels(planning.get("week_start", ""))
    # Période affichée : jours 1..nb_jours (les jours hors période sont masqués).
    nb_jours = _nb_jours(data)

    return templates.TemplateResponse(
        request,
        "planning.html",
        {
            "planning": planning,
            "titre": _week_label(planning.get("week_start", ""), plats),
            "plats": plats,
            "day_labels": day_labels,
            "week_rows": _group_week(plats),
            "week_nutrition": await _week_nutrition(plats, day_labels),
            "liste_courses": liste_courses,
            "courses_par_rayon": group_by_rayon(liste_courses),
            "repas_libres": _repas_libres(plats, day_labels),
            "courses_checked": data.get("courses_checked", []),
            "rayon_order": RAYON_ORDER,
            "per_day": per_day,
            "off_meals": data.get("off_meals", []),
            "nb_jours": nb_jours,
            "creneaux_non_resolus": data.get("creneaux_non_resolus", []),
            "briques_manquantes": data.get("briques_manquantes", ""),
            "valide": bool(planning.get("valide", 0)),
            "repas_options": REPAS_OPTIONS,
            "base_options": BASE_OPTIONS,
        },
    )


@router.post("/planning/{planning_id}/dupliquer")
async def dupliquer_planning(planning_id: int):
    """Recopie un planning existant en nouveau brouillon (sans liste de courses),
    dont le jour 1 est aujourd'hui. Permet de « refaire » une semaine appréciée."""
    src = await db.get_planning_with_recipes(planning_id)
    if not src:
        return {"error": "Planning introuvable"}
    data = json.loads(src["data_json"])
    plats = data.get("plats", [])
    off_meals = data.get("off_meals", [])
    # week_start = date du jour 1 : la copie démarre aujourd'hui, mêmes indices
    # de jour que l'original (plus de recalage sur le lundi).
    week_start = date.today().isoformat()
    # Grille du constructeur pour pouvoir « Reprendre » la copie ; reconstruite
    # depuis les plats pour les anciens plannings qui n'en ont pas.
    builder = data.get("builder")
    if not isinstance(builder, list) or not builder:
        builder = _builder_from_plats(plats, off_meals, src.get("nb_personnes") or 4)

    # Les brouillons multiples coexistent : on ne purge PAS les autres brouillons.
    new_data = {
        "plats": plats, "liste_courses": [], "per_day": data.get("per_day", ""),
        "off_meals": off_meals, "builder": builder,
    }
    if data.get("nb_jours"):
        new_data["nb_jours"] = data["nb_jours"]
    new_id = await db.save_planning(
        week_start=week_start,
        saison=src.get("saison", ""),
        nb_personnes=src.get("nb_personnes", 4),
        ingredients_force=src.get("ingredients_force", ""),
        data_json=json.dumps(new_data, ensure_ascii=False),
        recipes=_recipe_rows(plats),
    )
    return {"success": True, "planning_id": new_id}


@router.post("/planning/{planning_id}/valider")
async def valider_planning(planning_id: int):
    """Valide un brouillon de planning : il rejoint l'historique."""
    planning = await db.get_planning_with_recipes(planning_id)
    if not planning:
        return {"error": "Planning introuvable"}
    await db.mark_planning_valid(planning_id)
    return {"success": True}


@router.post("/planning/{planning_id}/supprimer")
async def supprimer_planning(planning_id: int):
    """Supprime un planning (brouillon ou validé) et ses recettes liées."""
    planning = await db.get_planning_with_recipes(planning_id)
    if not planning:
        return {"error": "Planning introuvable"}
    await db.delete_planning(planning_id)
    return {"success": True}


@router.get("/api/planning/{planning_id}/apercu")
async def api_planning_apercu(planning_id: int):
    """Aperçu compact d'une semaine (pour le survol dans l'historique) :
    par jour, le nom du repas midi/soir (plat + accompagnements résumés)."""
    planning = await db.get_planning_with_recipes(planning_id)
    if not planning:
        return {"error": "Introuvable"}
    try:
        plats = json.loads(planning["data_json"]).get("plats", [])
    except (json.JSONDecodeError, TypeError):
        plats = []
    # Libellés courts dérivés de la date du jour 1 (week_start), pas de l'index.
    day_labels = _day_labels(planning.get("week_start", ""))

    by = {(p.get("jour"), p.get("moment")): p for p in plats}
    jours = []
    for d in range(1, 8):
        midi, soir = by.get((d, "midi")), by.get((d, "soir"))
        if midi or soir:
            jours.append({
                "jour": day_labels[d - 1]["court"],
                "midi": _meal_label(midi) if midi else "",
                "soir": _meal_label(soir) if soir else "",
            })
    return {"jours": jours}


@router.post("/api/planning/{planning_id}/meals")
async def api_planning_meals(planning_id: int, request: Request):
    """Modifie PLUSIEURS créneaux en une requête, de façon atomique.

    Corps JSON :
      {"slots": [{"jour": 1-7, "moment": "midi"|"soir", "persons": int?}, ...],
       "action": "set_main" | "add_side" | "remove_side",
       "nom": str,            # recette (plat ou accompagnement)
       "remplace": str?}      # add_side : accompagnement à retirer d'abord

    - set_main : remplace le plat principal de chaque créneau (accompagnements
      remis à zéro). Un créneau ENCORE VIDE (y compris « absent ») est créé :
      convives = `persons` du slot, sinon ceux du jour, sinon du planning.
      Sert aussi à l'ajout depuis une fiche recette (L11). Un nom inconnu du
      catalogue Notion devient un repas libre.
    - add_side / remove_side : sur des créneaux qui ont déjà un plat.

    Toutes les mutations sont appliquées en mémoire, la liste de courses est
    régénérée une fois et le planning écrit une seule fois : en cas d'erreur
    (créneau manquant, exception…), rien n'est écrit.
    Réponse : {"success": true, "liste_courses": [...], "plats": [...]} ;
    erreurs : {"error": "..."} avec un statut 400 (corps invalide),
    404 (planning ou repas introuvable) ou 500."""
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "Corps JSON attendu"}, status_code=400)
    slots = _parse_slots(body.get("slots"))
    action = body.get("action")
    nom = str(body.get("nom") or "").strip()
    remplace = str(body.get("remplace") or "").strip()
    if slots is None or action not in MEAL_ACTIONS or not nom:
        return JSONResponse(
            {"error": "Paramètres invalides (slots jour 1-7 / moment midi|soir, action, nom)"},
            status_code=400,
        )

    planning = await db.get_planning_with_recipes(planning_id)
    if not planning:
        return JSONResponse({"error": "Planning introuvable"}, status_code=404)
    try:
        data = json.loads(planning["data_json"] or "{}")
        erreur = await apply_meals_batch(planning, data, slots, action, nom, remplace)
        if erreur:
            return JSONResponse({"error": erreur}, status_code=404)
        return await _refresh_shopping_and_save(planning, planning_id, data, data["plats"])
    except Exception as e:
        logger.exception("Erreur modification groupée des repas")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/alternatives/{planning_id}")
async def api_alternatives(planning_id: int):
    """Retourne les recettes disponibles non utilisées dans ce planning."""
    try:
        planning = await db.get_planning_with_recipes(planning_id)
        if not planning:
            return {"error": "Planning introuvable"}

        # Recettes déjà dans le planning
        used_names = {r["recipe_name"] for r in planning.get("recipes", [])}

        # Toutes les recettes Notion (briques comprises), hors celles déjà prises.
        recettes = await notion.get_all_recipes()
        return {"alternatives": _alternatives(recettes, used_names)}
    except Exception as e:
        logger.exception("Erreur récupération alternatives")
        return {"error": str(e), "alternatives": []}


@router.post("/api/update-meal/{planning_id}")
async def api_update_meal(
    planning_id: int,
    request: Request,
):
    """Remplace un repas dans le planning et regénère la liste de courses.

    Déprécié : utiliser POST /api/planning/{id}/meals (action set_main), qui
    traite plusieurs créneaux en une écriture."""

    data = await request.json()
    jour = data.get("jour")
    moment = data.get("moment")  # "midi" ou "soir"
    nouvelle_recette = (data.get("nouvelle_recette") or "").strip()

    if jour not in range(1, 8) or moment not in ("midi", "soir") or not nouvelle_recette:
        return {"error": "Paramètres invalides (jour 1-7, moment midi/soir, recette)"}

    try:
        # Récupérer le planning actuel et le plat à remplacer
        slot = await _load_slot(planning_id, jour, moment)
        if isinstance(slot, dict):
            return slot
        planning, planning_data, plats, target = slot

        r = _index_by_name(await notion.get_all_recipes()).get(nouvelle_recette.lower().strip())
        # Le plat principal change : on repart d'une assiette vide.
        _set_main(target, nouvelle_recette, r)
        # Nom inconnu au catalogue → repas libre (titre à la main, rappel courses).
        target["libre"] = not bool(r)

        # Regénérer la liste de courses + persister le planning (mêmes règles
        # que /generate-shopping : pas d'extraction LLM à l'aveugle, mise à
        # l'échelle par jour, titre de recette par ingrédient).
        return await _refresh_shopping_and_save(planning, planning_id, planning_data, plats)

    except Exception as e:
        logger.exception("Erreur update meal")
        return {"error": str(e)}


@router.post("/api/free-meal/{planning_id}")
async def api_free_meal(planning_id: int, request: Request):
    """Crée une recette minimale « repas libre » (nom + quelques ingrédients)
    puis la place dans un créneau du planning. Utile pour un plat qui n'existe
    pas encore en base (ex. « Steak + haricots verts »).

    JSON attendu : {jour:int, moment:str, nom:str, ingredients:str}
    (ingredients = texte multi-lignes, une ligne par ingrédient).
    """
    data = await request.json()
    jour = data.get("jour")
    moment = data.get("moment")  # "midi" ou "soir"
    nom = (data.get("nom") or "").strip()
    ingredients_text = data.get("ingredients") or ""

    if jour not in range(1, 8) or moment not in ("midi", "soir") or not nom:
        return {"error": "Paramètres invalides (jour 1-7, moment midi/soir, nom)"}

    try:
        # Récupérer le planning et repérer le créneau cible AVANT de créer la
        # recette (évite de créer une page Notion orpheline si le créneau
        # n'existe pas).
        slot = await _load_slot(planning_id, jour, moment)
        if isinstance(slot, dict):
            return slot
        planning, planning_data, plats, target = slot

        # Parser les ingrédients saisis (une ligne → {nom, quantite, unite}).
        structured = [
            i for i in (parse_ingredient_line(line) for line in ingredients_text.split("\n"))
            if i and i["nom"]
        ]

        # Créer la VRAIE recette minimale réutilisable dans Notion.
        result = await notion.create_recipe(nom=nom, repas="Plat")
        page_id = result.get("id", "")
        if not page_id:
            return {"error": "Création de la recette impossible"}

        # Écrire les ingrédients : Notion + cache local structuré (pour scaling,
        # nutrition et reconstruction de la liste de courses).
        if structured:
            await notion.update_ingredients(page_id, _ingredients_to_text(structured))
            await db.save_enriched(page_id, nom, ingredients=json.dumps(structured))

        # Placer la recette fraîchement créée dans le créneau : on construit le
        # dict recette nous-mêmes (on a déjà son id/url via create_recipe) plutôt
        # que de re-fetch tout le catalogue.
        # Nouveau plat principal : les accompagnements sont réinitialisés.
        _set_main(target, nom, {
            "id": page_id,
            "url": "",                        # pas d'URL source pour un repas libre
            "notion_url": result.get("url", ""),
            "repas": ["Plat"],
            "tags": [],
        })
        target["libre"] = False  # vraie recette Notion créée → plus un repas libre

        # Régénérer la liste de courses + persister EXACTEMENT comme /api/update-meal.
        return await _refresh_shopping_and_save(planning, planning_id, planning_data, plats)

    except Exception as e:
        logger.exception("Erreur repas libre")
        return {"error": str(e)}


@router.post("/api/add-side/{planning_id}")
async def api_add_side(planning_id: int, request: Request):
    """Ajoute (APPEND) un accompagnement au repas ciblé. Doublon (même nom)
    ignoré. JSON attendu : {jour:int, moment:str, nom:str}.

    Déprécié : utiliser POST /api/planning/{id}/meals (action add_side)."""
    data = await request.json()
    nom = (data.get("nom") or data.get("nouvelle_recette") or "").strip()
    if not nom:
        return {"error": "Nom d'accompagnement manquant"}

    async def mutate(accs: list[dict]) -> list[dict]:
        if any(a.get("nom_recette", "").lower().strip() == nom.lower().strip() for a in accs):
            return accs
        accs.append(await _resolve_side(nom))
        return accs

    return await _mutate_sides(planning_id, data.get("jour"), data.get("moment"), mutate)


@router.post("/api/remove-side/{planning_id}")
async def api_remove_side(planning_id: int, request: Request):
    """Retire UN accompagnement du repas ciblé (par nom).
    JSON attendu : {jour:int, moment:str, nom:str}.

    Déprécié : utiliser POST /api/planning/{id}/meals (action remove_side)."""
    data = await request.json()
    nom = (data.get("nom") or "").strip().lower()
    if not nom:
        return {"error": "Nom d'accompagnement manquant"}

    async def mutate(accs: list[dict]) -> list[dict]:
        return [a for a in accs if a.get("nom_recette", "").lower().strip() != nom]

    return await _mutate_sides(planning_id, data.get("jour"), data.get("moment"), mutate)


@router.post("/api/brique/{planning_id}")
async def api_brique(planning_id: int, request: Request):
    """Crée une « brique » (composant simple : steak, œuf, riz…) et la place
    dans un créneau du planning, en plat OU en accompagnement.

    Une brique = recette de Nature « Ingrédient » + une/des Base(s), dont
    l'ingrédient de courses est elle-même (son propre nom). Exclue de la
    génération auto mais réutilisable manuellement.

    JSON attendu : {jour:int, moment:str, slot:"plat"|"accompagnement",
    nom:str, base:list[str], quantite:str, unite:str}.
    """
    data = await request.json()
    jour = data.get("jour")
    moment = data.get("moment")  # "midi" ou "soir"
    slot = data.get("slot")      # "plat" ou "accompagnement"
    nom = (data.get("nom") or "").strip()
    base = data.get("base") or []
    if isinstance(base, str):
        base = [base]
    base = [b for b in base if b]
    quantite = (str(data.get("quantite") or "")).strip()
    unite = (str(data.get("unite") or "")).strip()

    if jour not in range(1, 8) or moment not in ("midi", "soir"):
        return {"error": "Paramètres invalides (jour 1-7, moment midi/soir)"}
    if slot not in ("plat", "accompagnement") or not nom:
        return {"error": "Paramètres invalides (slot plat/accompagnement, nom)"}

    try:
        # Repérer le créneau cible AVANT de créer la recette (évite une page
        # Notion orpheline si le créneau n'existe pas).
        found = await _load_slot(planning_id, jour, moment)
        if isinstance(found, dict):
            return found
        planning, planning_data, plats, target = found

        # Créer la recette brique (Nature=Ingrédient + Base, sans repas forcé).
        result = await notion.create_recipe(nom=nom, nature="Ingrédient", base=base)
        page_id = result.get("id", "")
        if not page_id:
            return {"error": "Création de la brique impossible"}

        # L'ingrédient de courses de la brique est elle-même.
        structured = _brique_ingredients(nom, quantite, unite)
        await notion.update_ingredients(page_id, _ingredients_to_text(structured))
        await db.save_enriched(page_id, nom, ingredients=json.dumps(structured))

        _place_brique(target, slot, nom, page_id, result.get("url", ""), base)
        return await _refresh_shopping_and_save(planning, planning_id, planning_data, plats)

    except Exception as e:
        logger.exception("Erreur brique")
        return {"error": str(e)}
