"""Sauvegarde et restauration des données locales (SQLite).

Les recettes vivent dans Notion, mais les plannings, listes de courses,
ingrédients structurés et réglages du foyer n'existent que dans la base
SQLite du volume `/data`. Ce module :

- exporte tout dans un JSON versionné (`export_all`) ;
- valide puis réimporte un tel fichier, en fusion ou en remplacement, dans une
  seule transaction (`validate_backup`, `import_all`) : forme des repas
  vérifiée, jeton de partage déjà pris retiré en fusion ;
- reconstruit le cache d'ingrédients depuis la copie texte que Notion garde de
  chaque recette (`rehydrate_from_notion`)."""

import json
import logging
from datetime import datetime
from typing import Any

from app.database import Database
from app.text_utils import parse_ingredient_lines

logger = logging.getLogger(__name__)

BACKUP_VERSION = 1
MODES = ("merge", "replace")


class BackupError(ValueError):
    """Fichier de sauvegarde invalide : rien n'a été modifié."""


async def export_all(db: Database) -> dict[str, Any]:
    """Toutes les données locales dans un dict sérialisable en JSON."""
    dump = await db.dump_all()
    return {
        "version": BACKUP_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        **dump,
    }


def export_filename(day: datetime | None = None) -> str:
    """Nom du fichier téléchargé : `menu-planner-AAAA-MM-JJ.json`."""
    return f"menu-planner-{(day or datetime.now()):%Y-%m-%d}.json"


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _check_plats(where: str, data: dict[str, Any]) -> None:
    """Repas d'un planning exporté : `plats` absent, ou liste d'objets
    {jour 1-7, moment midi|soir, nom_recette texte}. Sinon le planning
    importé ne s'afficherait pas."""
    if "plats" not in data:
        return
    plats = data["plats"]
    if not isinstance(plats, list):
        raise BackupError(f"{where} : plats doit être une liste")
    for k, plat in enumerate(plats):
        if (not isinstance(plat, dict) or not _is_int(plat.get("jour"))
                or not 1 <= plat["jour"] <= 7 or plat.get("moment") not in ("midi", "soir")
                or not isinstance(plat.get("nom_recette"), str)):
            raise BackupError(f"{where} : plat n°{k + 1} invalide (jour 1-7, moment midi/soir, nom_recette)")


def _check_planning(i: int, p: Any) -> dict[str, Any]:
    """Valide et normalise un planning exporté (data_json toujours en texte)."""
    where = f"planning n°{i + 1}"
    if not isinstance(p, dict):
        raise BackupError(f"{where} : objet attendu")
    if not _is_int(p.get("id")):
        raise BackupError(f"{where} : id entier manquant")
    if not isinstance(p.get("week_start"), str) or not p["week_start"]:
        raise BackupError(f"{where} : week_start manquant")
    data_json = p.get("data_json")
    if isinstance(data_json, dict):
        data_json = json.dumps(data_json, ensure_ascii=False)
    try:
        parsed = json.loads(data_json) if isinstance(data_json, str) else None
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, dict):
        raise BackupError(f"{where} : data_json illisible")
    _check_plats(where, parsed)
    recipes = p.get("recipes", [])
    if not isinstance(recipes, list):
        raise BackupError(f"{where} : recipes doit être une liste")
    for r in recipes:
        if (not isinstance(r, dict) or not isinstance(r.get("recipe_name"), str)
                or not _is_int(r.get("jour")) or not isinstance(r.get("moment"), str)):
            raise BackupError(f"{where} : repas invalide")
    nb = p.get("nb_personnes", 4)
    valide = p.get("valide", 1)
    if not _is_int(nb) or not _is_int(valide):
        raise BackupError(f"{where} : nb_personnes / valide entiers attendus")
    return {
        "id": p["id"],
        "week_start": p["week_start"],
        "created_at": str(p.get("created_at") or ""),
        "saison": p.get("saison") or "",
        "nb_personnes": nb,
        "ingredients_force": p.get("ingredients_force") or "",
        "data_json": data_json,
        "valide": 1 if valide else 0,
        "recipes": recipes,
    }


def _check_enriched(i: int, e: Any) -> dict[str, Any]:
    where = f"recette en cache n°{i + 1}"
    if not isinstance(e, dict):
        raise BackupError(f"{where} : objet attendu")
    if not isinstance(e.get("notion_id"), str) or not e["notion_id"]:
        raise BackupError(f"{where} : notion_id manquant")
    if not isinstance(e.get("recipe_name"), str):
        raise BackupError(f"{where} : recipe_name manquant")
    for col in ("cuisson_minutes", "base_servings"):
        if e.get(col) is not None and not _is_int(e[col]):
            raise BackupError(f"{where} : {col} entier attendu")
    return e


def validate_backup(data: Any) -> dict[str, Any]:
    """Vérifie un contenu de sauvegarde et le renvoie normalisé.

    Lève `BackupError` au premier défaut (version, structure, types)."""
    if not isinstance(data, dict):
        raise BackupError("Le fichier n'est pas une sauvegarde Menu Planner")
    if data.get("version") != BACKUP_VERSION:
        raise BackupError(f"Version de sauvegarde non prise en charge : {data.get('version')!r}")
    plannings, enriched = data.get("plannings"), data.get("enriched")
    prefs = data.get("prefs", {})
    if not isinstance(plannings, list) or not isinstance(enriched, list):
        raise BackupError("Sections « plannings » et « enriched » attendues (listes)")
    if not isinstance(prefs, dict):
        raise BackupError("Section « prefs » : objet attendu")
    plannings = [_check_planning(i, p) for i, p in enumerate(plannings)]
    ids = [p["id"] for p in plannings]
    if len(ids) != len(set(ids)):
        raise BackupError("Identifiants de planning en double")
    enriched = [_check_enriched(i, e) for i, e in enumerate(enriched)]
    nids = [e["notion_id"] for e in enriched]
    if len(nids) != len(set(nids)):
        raise BackupError("Recettes en cache en double")
    return {
        "version": BACKUP_VERSION,
        "exported_at": str(data.get("exported_at") or ""),
        "plannings": plannings,
        "enriched": enriched,
        "prefs": prefs,
    }


def parse_backup(raw: bytes | str) -> dict[str, Any]:
    """Décode puis valide le contenu brut d'un fichier de sauvegarde."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise BackupError("Fichier JSON illisible") from e
    return validate_backup(data)


def summarize(data: dict[str, Any]) -> dict[str, Any]:
    """Récapitulatif d'une sauvegarde validée (affiché avant confirmation)."""
    plannings = data["plannings"]
    return {
        "exported_at": data.get("exported_at", ""),
        "plannings": sum(1 for p in plannings if p["valide"]),
        "brouillons": sum(1 for p in plannings if not p["valide"]),
        "listes": sum(1 for p in plannings if json.loads(p["data_json"]).get("liste_courses")),
        "enriched": len(data["enriched"]),
        "prefs": sorted(data["prefs"]),
    }


async def import_all(db: Database, data: Any, mode: str = "merge") -> dict[str, int]:
    """Valide puis importe une sauvegarde, en une transaction.

    `mode="replace"` remplace toutes les données locales ; `mode="merge"`
    n'ajoute que ce qui manque (rien de local n'est écrasé). Un contenu
    invalide lève `BackupError` sans rien modifier."""
    if mode not in MODES:
        raise BackupError(f"Mode inconnu : {mode!r}")
    clean = validate_backup(data)
    if mode == "merge":
        await _retire_jetons_pris(db, clean["plannings"])
    return await db.restore_all(
        clean["plannings"], clean["enriched"], clean["prefs"], replace=(mode == "replace"),
    )


async def _retire_jetons_pris(db: Database, plannings: list[dict[str, Any]]) -> None:
    """Fusion : un planning importé dont le jeton de partage est déjà pris (par
    un planning local, ou par un planning importé avant lui) perd ce jeton,
    sinon un même lien public désignerait deux plannings. Le planning reste
    importé, sans lien de partage (à régénérer depuis sa page)."""
    vus: set[str] = set()
    for p in plannings:
        data = json.loads(p["data_json"])
        token = data.get("share_token")
        if not token:
            continue
        if str(token) in vus or await db.get_planning_by_share_token(str(token)):
            data.pop("share_token")
            p["data_json"] = json.dumps(data, ensure_ascii=False)
        else:
            vus.add(str(token))


def _has_ingredients(entry: dict[str, Any] | None) -> bool:
    """Vrai si l'entrée de cache porte au moins un ingrédient."""
    if not entry:
        return False
    try:
        return bool(json.loads(entry.get("ingredients") or "[]"))
    except (json.JSONDecodeError, TypeError):
        return True  # contenu inconnu : on le laisse tel quel


def ingredients_from_text(text: str) -> list[dict[str, str]]:
    """Ingrédients structurés depuis la copie texte Notion (une ligne chacun ;
    « sel, poivre » en donne deux)."""
    return [ing for line in (text or "").splitlines()
            for ing in parse_ingredient_lines(line) if ing.get("nom")]


async def rehydrate_from_notion(db: Database, notion: Any) -> dict[str, int]:
    """Remplit le cache local des recettes qui n'y ont pas d'ingrédients, à
    partir de la propriété Notion « Ingrédients ».

    N'écrase jamais des ingrédients présents : une entrée existante sans
    ingrédient reçoit seulement ce champ (durée, nutrition, portions gardées)."""
    recettes = await notion.get_all_recipes(force=True)
    stats = {"recettes": len(recettes), "restaurees": 0, "deja_en_cache": 0, "sans_texte": 0}
    for r in recettes:
        nid = r.get("id")
        if not nid:
            continue
        cached = await db.get_enriched(nid)
        if _has_ingredients(cached):
            stats["deja_en_cache"] += 1
            continue
        ings = ingredients_from_text(r.get("ingredients_text", ""))
        if not ings:
            stats["sans_texte"] += 1
            continue
        await db.save_enriched(
            nid, r.get("nom") or (cached or {}).get("recipe_name", ""),
            ingredients=json.dumps(ings, ensure_ascii=False),
            base_servings=None if cached else (r.get("base_servings") or 4),
        )
        stats["restaurees"] += 1
    logger.info("Réhydratation depuis Notion : %s", stats)
    return stats
