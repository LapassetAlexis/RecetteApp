"""Règles métier des recettes : statistiques de la liste, données de la fiche
(ingrédients en cache, portions, nutrition) et usage (« Cuisinée N fois »).

Le reste vit dans des modules dédiés : `recette_edition` (ajout, édition),
`enrichissement` (par lot, flux SSE), `recette_planifier` (ajout au planning
depuis la fiche). Les anciens noms restent importables d'ici (voir `compat`)."""

import json
import logging
from datetime import date

from app.config import (
    BASE_OPTIONS,
    NATURE_OPTIONS,
    REPAS_OPTIONS,
    TAG_OPTIONS,
    recipe_base,
    recipe_nature,
    recipe_types,
)
from app.deps import BASE_SERVINGS, db
from app.nutrition import estimate_nutrition
from app.services.compat import anciens_noms

logger = logging.getLogger(__name__)

_NUTRITION_KEYS = ("calories", "proteines", "glucides", "lipides")

# Moment (select Notion) : valeur stockée → libellé du formulaire.
MOMENT_OPTIONS = [("Les deux", "Midi et soir"), ("Midi", "Midi uniquement"), ("Soir", "Soir uniquement")]

# État (status Notion) : options connues de la base (cf. RATED_STATUS).
ETAT_OPTIONS = ["À essayer", "Prochaine recette", "Réussie", "Testée"]


def form_options() -> dict:
    """Valeurs proposées par les formulaires d'ajout et d'édition de recette."""
    return {
        "repas_options": REPAS_OPTIONS,
        "tag_options": TAG_OPTIONS,
        "base_options": BASE_OPTIONS,
        "nature_options": NATURE_OPTIONS,
        "moment_options": MOMENT_OPTIONS,
        "etat_options": ETAT_OPTIONS,
    }


def positive(value, default: int) -> int:
    """Entier strictement positif soumis, sinon `default`."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def recipe_stats(recettes: list[dict], ings_by_id: dict[str, str],
                 duree_by_id: dict[str, int]) -> dict:
    """Compteurs des filtres de la liste (type / base / état / tag / nature).

    Reporte aussi sur chaque recette ses ingrédients en cache (recherche par
    ingrédient) et sa durée (badge temps)."""
    par_type: dict[str, int] = {}
    par_base: dict[str, int] = {}
    par_etat: dict[str, int] = {}
    par_tag: dict[str, int] = {}
    par_nature: dict[str, int] = {}
    for r in recettes:
        r["ingredients_search"] = ings_by_id.get(r["id"], "")
        r["duree"] = duree_by_id.get(r["id"], 0)
        par_nature[recipe_nature(r)] = par_nature.get(recipe_nature(r), 0) + 1
        types = recipe_types(r) or ["Non classé"]
        for t in types:
            par_type[t] = par_type.get(t, 0) + 1
        for b in recipe_base(r):
            par_base[b] = par_base.get(b, 0) + 1
        if r["etat"]:
            par_etat[r["etat"]] = par_etat.get(r["etat"], 0) + 1
        for t in r["tags"]:
            par_tag[t] = par_tag.get(t, 0) + 1
    return {
        "total": len(recettes),
        "par_nature": par_nature,
        "par_type": sorted(par_type.items()),
        "par_base": sorted(par_base.items(), key=lambda kv: -kv[1]),
        "par_etat": sorted(par_etat.items()),
        "par_tag": sorted(par_tag.items(), key=lambda kv: -kv[1]),  # tags par fréquence
    }


def cached_ingredients(cached: dict | None) -> list[dict]:
    """Ingrédients structurés {nom, quantite (texte), unite} du cache local."""
    if not cached or not cached.get("ingredients"):
        return []
    try:
        return [
            {"nom": i.get("nom", ""), "quantite": str(i.get("quantite", "") or ""),
             "unite": i.get("unite", "")}
            for i in json.loads(cached["ingredients"]) if i.get("nom")
        ]
    except (json.JSONDecodeError, TypeError):
        return []


def detail_base_servings(cached: dict | None, recette: dict) -> int:
    """Base de portions d'une recette : priorité au cache, repli sur la valeur
    Notion (Portions) lue par _parse_page, défaut BASE_SERVINGS."""
    base_servings = BASE_SERVINGS
    try:
        if cached and cached.get("base_servings"):
            base_servings = int(cached["base_servings"])
        elif recette.get("base_servings"):
            base_servings = int(recette["base_servings"])
    except (TypeError, ValueError):
        base_servings = BASE_SERVINGS
    if base_servings <= 0:
        base_servings = BASE_SERVINGS
    return base_servings


def detail_nutrition(cached: dict | None, ingredients: list[dict], base_servings: int) -> dict | None:
    """Nutrition par part : valeurs exactes de la source (JSON-LD) si on les a
    stockées, sinon estimation calculée depuis les ingrédients."""
    if cached and cached.get("nutrition"):
        try:
            src = json.loads(cached["nutrition"]) or {}
        except (json.JSONDecodeError, TypeError):
            src = {}
        # n'utiliser la nutrition de la source que si COMPLÈTE (calories +
        # 3 macros) ; sinon (souvent juste calories, parfois fausses) on estime
        if src and all(src.get(k) is not None for k in _NUTRITION_KEYS):
            src.setdefault("source", "source")
            src.setdefault("confiance", "Excellente")  # valeurs officielles du site
            return src
    if ingredients:
        return estimate_nutrition(ingredients, base_servings)
    return None


# ── Usage des recettes (« Cuisinée N fois ») ─────────────────────────────


def usage_label(usage: dict | None) -> str:
    """« Cuisinée 3 fois · dernière fois le 12/10 » (année ajoutée si ce n'est
    pas l'année en cours) ; chaîne vide si la recette n'a jamais été planifiée."""
    if not usage or not usage.get("count"):
        return ""
    out = f"Cuisinée {int(usage['count'])} fois"
    try:
        d = date.fromisoformat(usage.get("last") or "")
    except (TypeError, ValueError):
        return out
    fmt = f"{d.day:02d}/{d.month:02d}" + ("" if d.year == date.today().year else f"/{d.year}")
    return f"{out} · dernière fois le {fmt}"


def attach_usage(recettes: list[dict], usage: dict[str, dict]) -> None:
    """Reporte sur chaque recette son usage (nombre, dernière date, libellé)."""
    for r in recettes:
        u = usage.get(r.get("id", "")) or {}
        r["usage_count"] = int(u.get("count") or 0)
        r["usage_last"] = u.get("last") or ""
        r["usage_label"] = usage_label(u)


async def recipe_usage() -> dict[str, dict]:
    """Usage de toutes les recettes, vide si la lecture échoue (best-effort)."""
    try:
        return await db.get_recipe_usage()
    except Exception as e:
        logger.warning(f"Lecture de l'usage des recettes échouée: {e}")
        return {}


__getattr__ = anciens_noms(__name__)
