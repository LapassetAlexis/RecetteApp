"""Planning construit depuis le constructeur (POST /construire) : plats des
cases actives, convives par jour, période, liste de courses, fusion avec un
planning existant ; et copie d'un planning (« Refaire »)."""

import json

from app.deps import db, notion
from app.services.planning import build_plat, build_side, nb_jours_periode
from app.services.planning_builder import builder_from_plats, parse_builder_meals
from app.services.shopping import collect_shopping
from app.services.shopping_list import finalize_items, purge_orphans
from app.text_utils import merge_ingredients


def json_or_none(raw: str):
    """Parse une chaîne JSON en objet Python, ou None si vide/illisible.
    Sert à re-stocker la grille brute du constructeur (`builder`)."""
    if not raw or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def plats_from_cases(actives: list[dict], by_id: dict[str, dict]) -> list[dict]:
    """Plats du planning depuis les cases actives du constructeur.

    Les midis d'un même groupe partagent le repas du 1er midi rempli du groupe
    (même couleur = même plat) ; une case sans plat principal est ignorée."""
    # Repas de référence des midis groupés : le 1er midi rempli du groupe.
    midi_group_meal: dict[int, dict] = {}
    for c in sorted((c for c in actives if c["moment"] == "midi"),
                    key=lambda c: c["jour"]):
        if c["main"] and c["group"] not in midi_group_meal:
            midi_group_meal[c["group"]] = c

    plats: list[dict] = []
    for c in actives:
        if c["moment"] == "midi":
            src = midi_group_meal.get(c["group"])
            if not src:
                continue  # aucun midi rempli dans ce groupe
            main_item, accs = src["main"], src["accompagnements"]
        else:
            if not c["main"]:
                continue
            main_item, accs = c["main"], c["accompagnements"]
        plat = build_plat(main_item, c["jour"], c["moment"], c["persons"], by_id)
        plat["accompagnements"] = [build_side(a, by_id) for a in accs]
        plats.append(plat)
    return plats


def per_day_from_cases(cases: list[dict], nb_personnes: int) -> list[int]:
    """Personnes par jour (1..7) : le plus grand nombre de convives du midi et
    du soir, repli sur `nb_personnes` pour un jour vide."""
    by_key = {(c["jour"], c["moment"]): c for c in cases}
    per_day_list = []
    for d in range(1, 8):
        pm = by_key.get((d, "midi"), {}).get("persons", 0)
        ps = by_key.get((d, "soir"), {}).get("persons", 0)
        per_day_list.append(max(pm, ps) or nb_personnes)
    return per_day_list


def periode_saisie(raw) -> int:
    """Durée de la période envoyée par le constructeur (champ `nb_jours`),
    1..7, ou 0 si absente ou invalide (repli sur les cases)."""
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return n if 1 <= n <= 7 else 0


def nb_jours_from_cases(cases: list[dict]) -> int:
    """Dernier jour de la période saisie dans le constructeur : les jours hors
    période sont envoyés « absents » (persons 0), donc dernier jour à convives.
    0 si aucune case active."""
    return max((c["jour"] for c in cases if c["persons"] > 0), default=0)


async def editable_planning(draft_id: str) -> dict | None:
    """Planning à mettre à jour par /construire (`draft_id` d'un planning
    existant, brouillon OU semaine validée rouverte via `/?planning=<id>`),
    sinon None. Une semaine validée reste validée : `update_planning` ne
    touche pas à la colonne `valide`."""
    try:
        did = int(draft_id) if draft_id else 0
    except (TypeError, ValueError):
        did = 0
    if did:
        return await db.get_planning_with_recipes(did)
    return None


def merge_existing_data(existing: dict | None, data: dict) -> dict:
    """Données d'un planning ré-enregistré depuis le constructeur : la nouvelle
    grille remplace plats, courses et période, mais les autres clés de
    `data_json` (lien de partage, articles ajoutés à la main…) sont conservées.
    Les coches de courses ne survivent que pour un article encore présent."""
    try:
        old = json.loads(existing["data_json"]) if existing else {}
    except (json.JSONDecodeError, TypeError, KeyError):
        old = {}
    if not isinstance(old, dict):
        old = {}
    merged = {**old, **data}
    # Purge par clé d'article (liste + articles manuels, anciennes coches par
    # nom tolérées).
    purge_orphans(merged)
    # Ces clés décrivent l'ancienne génération : elles ne valent plus.
    for k in ("creneaux_non_resolus", "briques_manquantes"):
        if k not in data:
            merged.pop(k, None)
    return merged


async def planning_from_builder(meals: str, nb_jours: str) -> tuple[dict, int] | None:
    """Données d'un planning depuis la grille du constructeur (champ `meals`).

    Renvoie `(data, nb_personnes)` — plats, convives par jour, absences, liste
    de courses rangée par rayon (clés d'article comprises), grille brute et
    période — ou None si aucun repas n'est renseigné. Lit le catalogue Notion :
    à appeler hors du verrou d'écriture."""
    cases = parse_builder_meals(meals)
    actives = [c for c in cases if c["persons"] > 0]
    by_id = {r["id"]: r for r in await notion.get_all_recipes()}
    plats = plats_from_cases(actives, by_id)
    if not plats:
        return None

    # Personnes de référence + per_day (repli d'affichage / anciens plannings).
    nb_personnes = max((c["persons"] for c in actives), default=4)
    per_day_list = per_day_from_cases(cases, nb_personnes)
    # Liste de courses ANCRÉE sur le cache DB, mise à l'échelle PAR REPAS.
    collected, _ = await collect_shopping(plats, per_day_list, nb_personnes)
    data = {
        "plats": plats,
        "liste_courses": finalize_items(merge_ingredients(collected)),
        "per_day": ",".join(str(x) for x in per_day_list),
        "off_meals": sorted(f"{c['jour']}:{c['moment']}" for c in cases if c["persons"] <= 0),
        # Grille brute du formulaire, pour ré-éditer ce brouillon.
        "builder": json_or_none(meals) or [],
        "nb_jours": (periode_saisie(nb_jours) or nb_jours_from_cases(cases)
                     or nb_jours_periode({"plats": plats})),
    }
    return data, nb_personnes


def copy_data(data: dict, nb_personnes: int = 4) -> dict:
    """Données d'un nouveau brouillon recopié d'un planning (« refaire » une
    semaine) : mêmes plats, convives, absences et période, sans liste de
    courses. La grille du constructeur est reprise, ou reconstruite depuis les
    plats pour les anciens plannings qui n'en ont pas (pour « Reprendre »)."""
    plats = data.get("plats", [])
    off_meals = data.get("off_meals", [])
    builder = data.get("builder")
    if not isinstance(builder, list) or not builder:
        builder = builder_from_plats(plats, off_meals, nb_personnes)
    new_data = {
        "plats": plats, "liste_courses": [], "per_day": data.get("per_day", ""),
        "off_meals": off_meals, "builder": builder,
    }
    if data.get("nb_jours"):
        new_data["nb_jours"] = data["nb_jours"]
    return new_data
