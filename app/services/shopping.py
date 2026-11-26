"""Liste de courses d'un planning : collecte des ingrédients en cache, mise à
l'échelle, fusion, rangement par rayon et enregistrement (génération).

La liste partagée (clés, coches, articles manuels, placard, vue, partage) vit
dans `shopping_list`."""

import inspect
import json
import logging

from app.deps import BASE_SERVINGS, db
from app.services import store
from app.services.planning import nb_jours_periode, per_day_list, plat_accompagnements
from app.services.planning_builder import builder_from_plats
from app.services.shopping_list import finalize_items, purge_orphans
from app.text_utils import (
    clean_recipe_title,
    merge_ingredients,
    normalize_cached_ingredient,
    scale_ingredients,
)

logger = logging.getLogger(__name__)


async def collect_shopping(
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
            # repas_libres, inutile de le signaler comme « non enrichi ».
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


async def refresh_shopping(planning: dict, planning_data: dict) -> dict:
    """Régénère, EN MÉMOIRE, la liste de courses (ancrée sur le cache DB) et la
    grille du constructeur depuis `planning_data["plats"]`. Renvoie la réponse
    standard des endpoints planning.

    Mêmes règles partout (/api/planning/{id}/meals, /api/free-meal,
    /api/brique, ajout depuis une fiche) : pas d'extraction LLM à l'aveugle,
    mise à l'échelle par repas, titre de recette par ingrédient, rayon."""
    plats = planning_data.setdefault("plats", [])
    per_day = per_day_list(planning, planning_data)
    collected, _ = await collect_shopping(
        plats, per_day, planning.get("nb_personnes", 4),
    )
    liste_courses = merge_ingredients(collected)
    finalize_items(liste_courses)
    planning_data["liste_courses"] = liste_courses
    # La grille du constructeur suit les plats : un brouillon modifié depuis
    # la page planning puis « Repris » garde les modifications, et les groupes
    # de midis choisis dans le constructeur (ancienne grille) sont conservés.
    planning_data["builder"] = builder_from_plats(
        plats, planning_data.get("off_meals"), planning.get("nb_personnes") or 4,
        previous=planning_data.get("builder"), nb_jours=nb_jours_periode(planning_data),
    )
    # Coches et masques orphelins : on ne garde que ceux d'un article encore présent.
    purge_orphans(planning_data)
    return {"success": True, "liste_courses": liste_courses, "plats": plats}


async def refresh_shopping_and_save(planning_id: int, mutate=None) -> dict:
    """Modifie les repas d'un planning puis régénère et enregistre, sous verrou.

    `mutate(planning, data)` (synchrone ou asynchrone, sans appel à Notion)
    modifie `data["plats"]` en place et renvoie un message d'erreur, ou None.
    En cas d'erreur, rien n'est écrit. `data_json` et `planning_recipes` sont
    écrits ensemble. Renvoie la réponse standard, ou {"error": …}."""
    async def fn(planning: dict, data: dict):
        if mutate is not None:
            erreur = mutate(planning, data)
            if inspect.isawaitable(erreur):
                erreur = await erreur
            if erreur:
                return store.NoWrite({"error": erreur})
        return await refresh_shopping(planning, data)

    data, res = await store.update_planning(planning_id, fn, recipes=True)
    if data is None:
        return {"error": "Planning introuvable"}
    return res


def ingredients_to_text(ings: list[dict]) -> str:
    """Formate les ingrédients en lignes propres « - 700 g d'épinards »."""
    out = []
    for i in ings:
        nom = (i.get("nom") or "").strip()
        if not nom:
            continue
        parts = [str(i.get("quantite", "")).strip(), str(i.get("unite", "")).strip(), nom]
        out.append("- " + " ".join(p for p in parts if p))
    return "\n".join(out)


def ing_lines(items: list[dict]) -> str:
    """Formate des ingrédients en lignes « - nom : qté unité »."""
    return "\n".join(
        f"- {i['nom']}" + (f" : {i.get('quantite','')} {i.get('unite','')}" if i.get('quantite') else "")
        for i in items
    )


# ── Rappel des repas libres (sans ingrédients en cache) ─────────────────


def repas_libres(plats: list[dict], day_labels: list[dict] | None = None) -> list[dict]:
    """Repas ET accompagnements libres du planning (titre saisi à la main, sans
    recette liée).

    Renvoie [{nom, jour, moment, quand}] : sert de rappel « pense à acheter les
    ingrédients » dans la liste de courses (ces repas ne contribuent aucun
    ingrédient au cache, cf. collect_shopping). `quand` = libellé humain
    (ex. « samedi soir ») dérivé de day_labels si fourni."""
    out: list[dict] = []
    for p in plats:
        jour, moment = p.get("jour"), p.get("moment")
        jour_lbl = ""
        try:
            if day_labels and 1 <= int(jour) <= len(day_labels):
                jour_lbl = day_labels[int(jour) - 1].get("long", "")
        except (TypeError, ValueError):
            pass
        quand = " ".join(x for x in (jour_lbl.lower(), moment) if x).strip()
        # Le plat, puis ses accompagnements libres (mêmes jour/moment).
        libres = [p] if p.get("libre") else []
        libres += [a for a in plat_accompagnements(p)
                   if a.get("libre") or not a.get("notion_id")]
        for src in libres:
            out.append({
                "nom": clean_recipe_title(src.get("nom_recette", "")),
                "jour": jour,
                "moment": moment,
                "quand": quand,
            })
    return out
