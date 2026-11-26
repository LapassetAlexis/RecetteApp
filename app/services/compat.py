"""Anciens noms des services, toujours importables depuis leur module d'origine.

Le découpage des services (M10) a déplacé des fonctions vers de nouveaux
modules et rendu publics les noms partagés entre modules. Les routeurs et les
tests importent encore les anciens noms (`from app.services.planning import
_builder_from_plats`) : chaque module d'origine les résout ici, À LA DEMANDE
(`__getattr__` de module), ce qui évite les imports circulaires entre un
module et ceux qui en ont été extraits.

À retirer (avec les `__getattr__` des modules) quand plus rien n'importe les
anciens noms (M11)."""

import importlib

# Module d'origine → {ancien nom: (module actuel, nom actuel)}. Module actuel
# vide = le module d'origine lui-même (simple renommage).
_ANCIENS: dict[str, dict[str, tuple[str, str]]] = {
    "planning": {
        "_index_by_name": ("", "index_by_name"),
        "_group_week": ("", "group_week"),
        "_per_day_list": ("", "per_day_list"),
        "_day_labels": ("", "day_labels"),
        "_week_label": ("", "week_label"),
        "_attach_label": ("", "attach_label"),
        "_build_plat": ("", "build_plat"),
        "_build_side": ("", "build_side"),
        "_recipe_rows": ("", "recipe_rows"),
        "_mark_non_enrichis": ("", "mark_non_enrichis"),
        "_nb_jours": ("", "nb_jours_periode"),
        "_repas_libres": ("shopping", "repas_libres"),
        "_alternatives": ("planning_batch", "alternatives"),
        "GROUPES_MAX": ("planning_builder", "GROUPES_MAX"),
        "_parse_builder_meals": ("planning_builder", "parse_builder_meals"),
        "_builder_from_plats": ("planning_builder", "builder_from_plats"),
        "_json_or_none": ("planning_construire", "json_or_none"),
        "_plats_from_cases": ("planning_construire", "plats_from_cases"),
        "_per_day_from_cases": ("planning_construire", "per_day_from_cases"),
        "_periode_saisie": ("planning_construire", "periode_saisie"),
        "_nb_jours_from_cases": ("planning_construire", "nb_jours_from_cases"),
        "_editable_planning": ("planning_construire", "editable_planning"),
        "_merge_existing_data": ("planning_construire", "merge_existing_data"),
        "planning_from_builder": ("planning_construire", "planning_from_builder"),
        "copy_data": ("planning_construire", "copy_data"),
        "MEAL_ACTIONS": ("planning_batch", "MEAL_ACTIONS"),
        "_parse_slots": ("planning_batch", "parse_slots"),
        "_resolve_side": ("planning_batch", "resolve_side"),
        "resolve_meal_action": ("planning_batch", "resolve_meal_action"),
        "apply_meals_batch": ("planning_batch", "apply_meals_batch"),
        "_brique_ingredients": ("planning_batch", "brique_ingredients"),
        "_place_brique": ("planning_batch", "place_brique_repas"),
    },
    "shopping": {
        "_collect_shopping": ("", "collect_shopping"),
        "_refresh_shopping_and_save": ("", "refresh_shopping_and_save"),
        "_ingredients_to_text": ("", "ingredients_to_text"),
        "_ing_lines": ("", "ing_lines"),
    },
    "recettes": {
        "_form_options": ("", "form_options"),
        "_recipe_stats": ("", "recipe_stats"),
        "_cached_ingredients": ("", "cached_ingredients"),
        "_detail_base_servings": ("", "detail_base_servings"),
        "_detail_nutrition": ("", "detail_nutrition"),
        "_usage_label": ("", "usage_label"),
        "_attach_usage": ("", "attach_usage"),
        "_recipe_usage": ("", "recipe_usage"),
        "_positive": ("", "positive"),
        "_analysis_response": ("recette_edition", "analysis_response"),
        "_enrichir_context": ("recette_edition", "enrichir_context"),
        "_parse_lines": ("recette_edition", "parse_lines"),
        "_enrichir_enregistrer": ("recette_edition", "enrichir_enregistrer"),
        "_ajouter": ("recette_edition", "ajouter"),
        "_planning_recipes": ("enrichissement", "planning_recipes"),
        "_enrich_one": ("enrichissement", "enrich_one"),
        "_enrich_many": ("enrichissement", "enrich_many"),
        "_enrich_all_events": ("enrichissement", "enrich_all_events"),
        "_MOMENT_ORDER": ("recette_planifier", "MOMENT_ORDER"),
        "_draft_choices": ("recette_planifier", "draft_choices"),
        "_draft_choices_safe": ("recette_planifier", "draft_choices_safe"),
        "_new_draft": ("recette_planifier", "new_draft"),
        "_planifier": ("recette_planifier", "planifier"),
    },
}

# Module d'origine → module où chercher, sous le même nom, un nom absent de la
# table (fonctions déplacées sans être renommées).
_DELEGUES = {"shopping": "shopping_list"}


def _module(court: str):
    return importlib.import_module(f"app.services.{court}")


def anciens_noms(module: str):
    """`__getattr__` du module `app.services.<court>` qui résout ses anciens noms."""
    court = module.rsplit(".", 1)[-1]
    table = _ANCIENS.get(court, {})
    delegue = _DELEGUES.get(court)

    def __getattr__(name: str):
        if name in table:
            cible, nouveau = table[name]
            return getattr(_module(cible or court), nouveau)
        if delegue and not name.startswith("__") and hasattr(_module(delegue), name):
            return getattr(_module(delegue), name)
        raise AttributeError(f"module {module!r} has no attribute {name!r}")

    return __getattr__


def table_anciens_noms() -> dict[str, list[str]]:
    """Tous les anciens noms de la table, par module (pour les tests)."""
    return {f"app.services.{m}": sorted(t) for m, t in _ANCIENS.items()}
