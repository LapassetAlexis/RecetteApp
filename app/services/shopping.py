"""Liste de courses d'un planning : collecte des ingrédients en cache, mise à
l'échelle, fusion, rangement par rayon et enregistrement."""

import json
import logging

from app.categories import categorize
from app.deps import BASE_SERVINGS, db
from app.services.planning import (
    _builder_from_plats,
    _load_slot,
    _per_day_list,
    _recipe_rows,
    plat_accompagnements,
)
from app.text_utils import clean_recipe_title, merge_ingredients, normalize_cached_ingredient, scale_ingredients

logger = logging.getLogger(__name__)


async def _collect_shopping(
    plats: list[dict], per_day: list[int], nb_personnes: int,
) -> tuple[list[dict], list[str]]:
    """Construit les ingrédients de courses ANCRÉS sur le cache DB par recette.

    - une recette contribue UNIQUEMENT ses ingrédients enregistrés (aucune
      extraction LLM à l'aveugle → pas d'ingrédients hallucinés) ;
    - les quantités sont mises à l'échelle selon le nb de personnes du REPAS
      (`plat.persons`), avec repli sur le nb du jour (`per_day`) puis
      `nb_personnes` pour les anciens plannings (× pers / BASE_SERVINGS) ;
    - chaque ingrédient porte le titre de sa recette source (`recette`).

    Retourne (ingrédients, titres des recettes non enrichies à signaler).
    """
    def _persons_for(src: dict) -> int:
        # Priorité au nb de personnes du repas (grille), sinon repli par jour.
        p = src.get("persons")
        try:
            if p is not None and int(p) > 0:
                return int(p)
        except (TypeError, ValueError):
            pass
        try:
            j = int(src.get("jour", 0))
        except (TypeError, ValueError):
            j = 0
        if per_day and 1 <= j <= len(per_day):
            return per_day[j - 1]
        return nb_personnes

    # Plats + accompagnements (chaque accompagnement hérite du jour ET du nb de
    # personnes de son plat).
    sources: list[dict] = []
    for p in plats:
        sources.append(p)
        for acc in plat_accompagnements(p):
            sources.append({**acc, "jour": p.get("jour"), "persons": p.get("persons")})

    collected: list[dict] = []
    non_enrichis: list[str] = []
    for src in sources:
        nid = src.get("notion_id", "")
        title = clean_recipe_title(src.get("nom_recette", ""))
        cached = await db.get_enriched(nid) if nid else None
        ings = None
        if cached and cached.get("ingredients"):
            try:
                ings = [i for i in json.loads(cached["ingredients"]) if i.get("nom")]
            except (json.JSONDecodeError, TypeError):
                ings = None
        if not ings:
            # Libre (plat ou accompagnement saisi à la main) : déjà rappelé par
            # _repas_libres, inutile de le signaler comme « non enrichi ».
            if not (src.get("libre") or not nid):
                non_enrichis.append(title or src.get("nom_recette", ""))
            continue
        # Factorisation en aval : re-normalise (unités qui avaient fui dans le
        # nom sur d'anciens caches) et éclate les listes de condiments.
        clean: list[dict] = []
        for ing in ings:
            clean.extend(normalize_cached_ingredient(ing))
        # Diviseur = base de portions PROPRE à la recette (colonne cache), pas la
        # constante globale : une recette « pour 1 » mise pour 2 pers → ×2.
        try:
            base = int(cached.get("base_servings") or BASE_SERVINGS)
        except (TypeError, ValueError):
            base = BASE_SERVINGS
        factor = _persons_for(src) / (base or BASE_SERVINGS)
        for ing in scale_ingredients(clean, factor):
            ing["recette"] = title
            collected.append(ing)
    return collected, non_enrichis


async def _refresh_shopping_and_save(
    planning: dict, planning_id: int, planning_data: dict, plats: list[dict],
) -> dict:
    """Régénère la liste de courses (ancrée sur le cache DB), la range dans le
    planning, persiste et renvoie la réponse standard des endpoints planning.

    Mutualise la fin de /api/update-meal, /api/free-meal et /api/brique :
    mêmes règles (pas d'extraction LLM à l'aveugle, mise à l'échelle par jour,
    titre de recette par ingrédient, catégorisation par rayon)."""
    per_day = _per_day_list(planning, planning_data)
    collected, _ = await _collect_shopping(
        plats, per_day, planning.get("nb_personnes", 4),
    )
    liste_courses = merge_ingredients(collected)
    for it in liste_courses:
        it["rayon"] = categorize(it.get("nom", ""))
    planning_data["liste_courses"] = liste_courses
    # La grille du constructeur suit les plats : un brouillon modifié depuis
    # la page planning puis « Repris » garde les modifications.
    planning_data["builder"] = _builder_from_plats(
        plats, planning_data.get("off_meals"), planning.get("nb_personnes") or 4,
    )
    # Coches orphelines : on ne garde que celles d'un article encore présent.
    if planning_data.get("courses_checked"):
        noms = {str(it.get("nom", "")).strip().lower() for it in liste_courses}
        planning_data["courses_checked"] = [
            c for c in planning_data["courses_checked"] if str(c).lower() in noms
        ]

    await db.update_planning(
        planning_id=planning_id,
        data_json=json.dumps(planning_data, ensure_ascii=False),
        recipes=_recipe_rows(plats),
    )
    return {"success": True, "liste_courses": liste_courses, "plats": plats}


async def _mutate_sides(planning_id: int, jour, moment, mutate):
    """Charge le planning, applique `mutate(cible)` sur la liste d'accompagnements
    du repas ciblé, régénère la liste de courses et persiste. Renvoie la réponse
    standard des endpoints planning (success/liste_courses/plats)."""
    if jour not in range(1, 8) or moment not in ("midi", "soir"):
        return {"error": "Paramètres invalides (jour 1-7, moment midi/soir)"}
    try:
        slot = await _load_slot(planning_id, jour, moment)
        if isinstance(slot, dict):
            return slot
        planning, planning_data, plats, cible = slot

        accs = plat_accompagnements(cible)
        accs = await mutate(accs)
        cible["accompagnements"] = accs
        cible.pop("accompagnement", None)  # migre l'ancien champ singulier

        return await _refresh_shopping_and_save(planning, planning_id, planning_data, plats)
    except Exception as e:
        logger.exception("Erreur mutation accompagnements")
        return {"error": str(e)}


def _ingredients_to_text(ings: list[dict]) -> str:
    """Formate les ingrédients en lignes propres « - 700 g d'épinards »."""
    out = []
    for i in ings:
        nom = (i.get("nom") or "").strip()
        if not nom:
            continue
        parts = [str(i.get("quantite", "")).strip(), str(i.get("unite", "")).strip(), nom]
        out.append("- " + " ".join(p for p in parts if p))
    return "\n".join(out)


def _ing_lines(items: list[dict]) -> str:
    """Formate des ingrédients en lignes « - nom : qté unité »."""
    return "\n".join(
        f"- {i['nom']}" + (f" : {i.get('quantite','')} {i.get('unite','')}" if i.get('quantite') else "")
        for i in items
    )
