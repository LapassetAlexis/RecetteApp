"""Routes du planning : construction depuis la grille, affichage, cycle de vie
(dupliquer, valider, supprimer) et modifications des repas."""

import json
import logging
from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import BASE_OPTIONS, REPAS_OPTIONS
from app.deps import db, notion, templates
from app.http import ERREUR_NOTION, depuis_service, erreur, erreur_exception, externe, lire_json, page_erreur
from app.services import store
from app.services.creations import place_brique, place_free_meal
from app.services.nutrition_week import _week_nutrition
from app.services.planning import (
    MEAL_ACTIONS,
    _alternatives,
    _day_labels,
    _editable_planning,
    _group_week,
    _json_or_none,
    _mark_non_enrichis,
    _merge_existing_data,
    _nb_jours,
    _parse_slots,
    _per_day_list,
    _periode_saisie,
    _recipe_rows,
    _repas_libres,
    _week_label,
    apercu_jours,
    apply_meals_batch,
    copy_data,
    parse_week_start,
    planning_from_builder,
    resolve_meal_action,
)
from app.services.shopping import _refresh_shopping_and_save, load_shopping_view

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/construire")
async def construire(
    request: Request,
    week_start: str = Form(...),
    meals: str = Form(""),
    action: str = Form("valider"),
    draft_id: str = Form(""),
    nb_jours: str = Form(""),
):
    """Construit un planning À LA MAIN depuis la grille du formulaire.

    Le champ caché `meals` (JSON) porte les cases de la semaine :
    [{jour 1-7, moment midi|soir, persons (0 = absent), group (midis),
      main {notion_id, nom, nature} | null,
      accompagnements [{notion_id, nom}, ...]}].
    Cases sans `main` ou avec persons <= 0 → ignorées. Les midis d'un même
    groupe partagent le repas du 1er midi rempli du groupe (même couleur =
    même plat). On sauvegarde un BROUILLON + sa liste de courses
    (cf. `planning_from_builder`).

    `week_start` : date du jour 1 (AAAA-MM-JJ) ; invalide → formulaire
    ré-affiché avec la saisie, rien n'est créé.
    `action` :
      - « valider » (défaut) → redirige vers la page planning (flux existant) ;
      - « brouillon » → reste en mode brouillon, redirige vers `/?draft=<id>`.
    `draft_id` (optionnel) : si fourni, on MET À JOUR ce planning (sous son
    verrou) au lieu d'en créer un nouveau. Les brouillons multiples
    coexistent (aucune purge). Une semaine validée rouverte (`/?planning=<id>`)
    reste validée, et « brouillon » renvoie alors vers `/?planning=<id>`.
    `nb_jours` (1..7) : durée de la période choisie dans le constructeur
    (un dernier jour entièrement absent reste dans la période). Sans ce champ
    (anciens clients) : dernier jour à convives."""

    def _error(message: str, status: int = 400):
        # On repasse la grille saisie (builder) + le draft_id pour ne pas perdre
        # la saisie de l'utilisateur en cas d'erreur.
        return templates.TemplateResponse(request, "index.html", {
            "error": message,
            "week_start": week_start,
            "builder": _json_or_none(meals) or [],
            "draft_id": draft_id,
            "nb_jours": _periode_saisie(nb_jours),
            "saved": False,
        }, status_code=status)

    debut = parse_week_start(week_start)
    if not debut:
        return _error("Date de début invalide (format AAAA-MM-JJ attendu).")
    try:
        built = await planning_from_builder(meals, nb_jours)
        if built is None:
            return _error("Aucun repas renseigné. Choisis au moins un plat principal.")
        data, nb_personnes = built

        # Mise à jour d'un planning existant (draft_id valide) plutôt que
        # création d'un nouveau : les autres clés de data_json sont gardées.
        existing = await _editable_planning(draft_id)
        planning_id = 0
        if existing:
            def merge(planning: dict, old: dict) -> None:
                merged = _merge_existing_data(planning, data)
                old.clear()
                old.update(merged)

            written, _ = await store.update_planning(
                existing["id"], merge, recipes=True,
                week_start=debut, nb_personnes=nb_personnes,
            )
            planning_id = existing["id"] if written is not None else 0
        if not planning_id:
            existing = None
            planning_id = await db.save_planning(
                week_start=debut, saison="", nb_personnes=nb_personnes,
                ingredients_force="", data_json=json.dumps(data, ensure_ascii=False),
                recipes=_recipe_rows(data["plats"]),
            )

        if action == "brouillon":
            # On RESTE dans le constructeur pré-rempli (semaine validée : elle
            # le reste, on la rouvre par ?planning=).
            param = "planning" if existing and existing.get("valide", 0) else "draft"
            return RedirectResponse(url=f"/?{param}={planning_id}&saved=1", status_code=303)
        return RedirectResponse(url=f"/planning/{planning_id}", status_code=303)

    except Exception as e:
        logger.exception("Erreur lors de la construction du planning")
        return _error("Enregistrement impossible : réessaie dans un instant.", 502 if externe(e) else 500)


def _repas_lisible(p) -> bool:
    return (isinstance(p, dict) and type(p.get("jour")) is int and 1 <= p["jour"] <= 7
            and p.get("moment") in ("midi", "soir") and isinstance(p.get("nom_recette"), str))


def _data_lisible(raw) -> dict | None:
    """`data_json` s'il a la forme attendue (`plats` : liste de repas {jour
    1-7, moment midi|soir, nom_recette}), sinon None."""
    try:
        data = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    ok = isinstance(data, dict) and isinstance(data.setdefault("plats", []), list)
    return data if ok and all(_repas_lisible(p) for p in data["plats"]) else None


@router.get("/planning/{planning_id}", response_class=HTMLResponse)
async def voir_planning(request: Request, planning_id: int):
    """Affiche un planning existant."""
    planning = await db.get_planning_with_recipes(planning_id)
    if not planning:
        return RedirectResponse(url="/")

    data = _data_lisible(planning.get("data_json"))
    if data is None:
        logger.warning("Planning %s illisible (data_json mal formé) : page 422", planning_id)
        return page_erreur(request, templates, "Ce planning est illisible : ses repas sont mal formés. "
                           "Supprime-le depuis l'historique ou restaure une sauvegarde.", 422,
                           titre="Planning illisible")
    plats = data["plats"]

    # Repas sans ingrédients en cache (« recette pas terminée ») : avertissement.
    _mark_non_enrichis(plats, set(await db.get_all_enriched_ingredients()))

    # Libellés des 7 jours dérivés de la date du jour 1 (week_start).
    day_labels = _day_labels(planning.get("week_start", ""))

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
            "repas_libres": _repas_libres(plats, day_labels),
            "shop": await load_shopping_view(data),
            "per_day": _per_day_list(planning, data),
            "off_meals": data.get("off_meals", []),
            # Période affichée : jours 1..nb_jours (les jours hors période sont masqués).
            "nb_jours": _nb_jours(data),
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
        return erreur("Planning introuvable", 404)
    new_data = copy_data(json.loads(src["data_json"]), src.get("nb_personnes") or 4)
    # La copie démarre aujourd'hui (jour 1), mêmes indices de jour que l'original.
    # Les brouillons multiples coexistent : on ne purge PAS les autres brouillons.
    new_id = await db.save_planning(
        week_start=date.today().isoformat(),
        saison=src.get("saison", ""),
        nb_personnes=src.get("nb_personnes", 4),
        ingredients_force=src.get("ingredients_force", ""),
        data_json=json.dumps(new_data, ensure_ascii=False),
        recipes=_recipe_rows(new_data["plats"]),
    )
    return {"success": True, "planning_id": new_id}


@router.post("/planning/{planning_id}/valider")
async def valider_planning(planning_id: int):
    """Valide un brouillon de planning : il rejoint l'historique."""
    planning = await db.get_planning_with_recipes(planning_id)
    if not planning:
        return erreur("Planning introuvable", 404)
    await db.mark_planning_valid(planning_id)
    return {"success": True}


@router.post("/planning/{planning_id}/supprimer")
async def supprimer_planning(planning_id: int):
    """Supprime un planning (brouillon ou validé) et ses recettes liées."""
    planning = await db.get_planning_with_recipes(planning_id)
    if not planning:
        return erreur("Planning introuvable", 404)
    await db.delete_planning(planning_id)
    return {"success": True}


@router.get("/api/planning/{planning_id}/apercu")
async def api_planning_apercu(planning_id: int):
    """Aperçu compact d'une semaine (pour le survol dans l'historique) :
    par jour, le nom du repas midi/soir (plat + accompagnements résumés)."""
    planning = await db.get_planning_with_recipes(planning_id)
    if not planning:
        return erreur("Planning introuvable", 404)
    try:
        plats = json.loads(planning["data_json"]).get("plats", [])
    except (json.JSONDecodeError, TypeError):
        plats = []
    return {"jours": apercu_jours(plats, planning.get("week_start", ""))}


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
    body = await lire_json(request)
    slots = _parse_slots(body.get("slots"))
    action = body.get("action")
    nom = str(body.get("nom") or "").strip()
    remplace = str(body.get("remplace") or "").strip()
    if slots is None or action not in MEAL_ACTIONS or not nom:
        return erreur("Paramètres invalides (slots jour 1-7 / moment midi|soir, action, nom)")

    try:
        # Lecture Notion hors verrou, puis relecture + mutation + écriture sous verrou.
        resolved = await resolve_meal_action(action, nom)
        res = await _refresh_shopping_and_save(
            planning_id,
            lambda planning, data: apply_meals_batch(
                planning, data, slots, action, nom, remplace, resolved),
        )
        return depuis_service(res, 404)
    except Exception as e:
        logger.exception("Erreur modification groupée des repas")
        return erreur_exception(e)


@router.get("/api/alternatives/{planning_id}")
async def api_alternatives(planning_id: int):
    """Retourne les recettes disponibles non utilisées dans ce planning."""
    planning = await db.get_planning_with_recipes(planning_id)
    if not planning:
        return erreur("Planning introuvable", 404)
    used_names = {r["recipe_name"] for r in planning.get("recipes", [])}
    try:
        # Toutes les recettes Notion (briques comprises), hors celles déjà prises.
        recettes = await notion.get_all_recipes()
    except Exception:
        logger.exception("Erreur récupération alternatives")
        return erreur(ERREUR_NOTION, 502)
    return {"alternatives": _alternatives(recettes, used_names)}


@router.post("/api/free-meal/{planning_id}")
async def api_free_meal(planning_id: int, request: Request):
    """Crée une recette minimale « repas libre » (nom + quelques ingrédients)
    puis la place dans un ou plusieurs créneaux du planning. Utile pour un plat
    qui n'existe pas encore en base (ex. « Steak + haricots verts »).

    JSON attendu : {slots:[{jour, moment}, …] | jour:int + moment:str,
    nom:str, ingredients:str} (ingredients = une ligne par ingrédient).
    Plusieurs créneaux (case fusionnée) : une seule recette créée, un seul
    enregistrement ; rien n'est écrit si un créneau manque."""
    body = await lire_json(request)
    try:
        return depuis_service(await place_free_meal(planning_id, body))
    except Exception as e:
        logger.exception("Erreur repas libre")
        return erreur_exception(e)


@router.post("/api/brique/{planning_id}")
async def api_brique(planning_id: int, request: Request):
    """Crée une « brique » (composant simple : steak, œuf, riz…) et la place
    dans un ou plusieurs créneaux du planning, en plat OU en accompagnement.

    Une brique = recette de Nature « Ingrédient » + une/des Base(s), dont
    l'ingrédient de courses est elle-même (son propre nom). Exclue de la
    génération auto mais réutilisable manuellement.

    JSON attendu : {slots:[{jour, moment}, …] | jour:int + moment:str,
    slot:"plat"|"accompagnement", nom:str, base:list[str], quantite:str,
    unite:str}. Plusieurs créneaux : une seule brique créée, un seul
    enregistrement ; rien n'est écrit si un créneau manque.
    """
    body = await lire_json(request)
    try:
        return depuis_service(await place_brique(planning_id, body))
    except Exception as e:
        logger.exception("Erreur brique")
        return erreur_exception(e)
