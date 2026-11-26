"""Ajout d'une recette au planning depuis sa fiche : brouillons proposés par
la modale « Ajouter au planning », brouillon neuf d'un jour, placement dans un
créneau (une seule écriture du planning, sous son verrou)."""

import json
import logging
from datetime import date

from app.deps import BASE_SERVINGS, db, notion
from app.services.planning import attach_label, day_labels
from app.services.planning_batch import apply_meals_batch
from app.services.recettes import positive
from app.services.shopping import refresh_shopping_and_save

logger = logging.getLogger(__name__)

MOMENT_ORDER = {"midi": 0, "soir": 1}


def draft_choices(drafts: list[dict]) -> list[dict]:
    """Brouillons proposés par la modale « Ajouter au planning » : libellé,
    jours (dérivés du jour 1) et créneaux déjà occupés {"jour:moment": nom}."""
    out: list[dict] = []
    for d in drafts:
        attach_label(d)
        try:
            data = json.loads(d.get("data_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            data = {}
        occupied = {f"{p.get('jour')}:{p.get('moment')}": p.get("nom_recette", "")
                    for p in data.get("plats", []) if isinstance(p, dict)}
        days = [{"jour": j["jour"], "label": f"{j['long']} {j['date']}".strip()}
                for j in day_labels(d.get("week_start", ""))]
        out.append({"id": d["id"], "label": d["label"], "days": days, "occupied": occupied})
    return out


async def draft_choices_safe() -> list[dict]:
    """Brouillons de la modale, liste vide si la base est illisible."""
    try:
        return draft_choices(await db.list_drafts())
    except Exception as e:
        logger.warning(f"Lecture des brouillons échouée: {e}")
        return []


async def new_draft(start: str, persons: int) -> int:
    """Crée un brouillon vide d'un jour démarrant le `start` (AAAA-MM-JJ)."""
    data = {"plats": [], "liste_courses": [], "per_day": ",".join([str(persons)] * 7),
            "off_meals": [], "builder": [], "nb_jours": 1}
    return await db.save_planning(
        week_start=start, saison="", nb_personnes=persons, ingredients_force="",
        data_json=json.dumps(data, ensure_ascii=False), recipes=[],
    )


async def planifier(page_id: str, body: dict) -> dict:
    """Place une recette dans un brouillon : `planning_id` + `jour` (1-7) +
    `moment`, ou (`planning_id` absent ou « new ») un nouveau brouillon d'un
    jour démarrant à `date` (AAAA-MM-JJ, défaut aujourd'hui). Le plat du
    créneau est créé ou remplacé (même règle que POST /api/planning/{id}/meals,
    action set_main), puis la liste de courses et la grille du constructeur
    sont régénérées : une seule écriture du planning, sous son verrou.

    Le brouillon neuf n'est créé qu'une fois les paramètres et la recette
    validés, et il est supprimé si l'écriture échoue.

    Retourne {success, planning_id, url} ou {error}."""
    moment = body.get("moment")
    if moment not in MOMENT_ORDER:
        return {"error": "Moment invalide (midi ou soir)"}
    nouveau = body.get("planning_id") in (None, "", "new")
    try:
        jour = 1 if nouveau else int(body.get("jour"))
        start = date.fromisoformat(body.get("date") or date.today().isoformat()) if nouveau else None
        planning_id = 0 if nouveau else int(body.get("planning_id"))
    except (TypeError, ValueError):
        return {"error": "Paramètres invalides (brouillon, jour 1 à 7, date AAAA-MM-JJ)"}
    if not 1 <= jour <= 7:
        return {"error": "Jour invalide (1 à 7)"}

    recette = await notion.get_recipe(page_id)
    if not recette:
        return {"error": "Recette introuvable"}

    slot = {"jour": jour, "moment": moment, "persons": positive(body.get("persons"), 0)}
    if nouveau:
        planning_id = await new_draft(start.isoformat(), positive(body.get("persons"), BASE_SERVINGS))

    def mutate(planning: dict, data: dict) -> str | None:
        if planning.get("valide"):
            return "Brouillon introuvable"
        return apply_meals_batch(planning, data, [slot], "set_main", recette["nom"],
                                 resolved=recette)

    try:
        res = await refresh_shopping_and_save(planning_id, mutate)
    except Exception:
        if nouveau:
            await db.delete_planning(planning_id)
        raise
    if res.get("error"):
        if nouveau:
            await db.delete_planning(planning_id)
        return {"error": "Brouillon introuvable" if res["error"] == "Planning introuvable"
                else res["error"]}
    planning = await db.get_planning_with_recipes(planning_id) or {}
    # Entrée à jour de la modale (créneaux occupés, nouveau brouillon).
    draft = draft_choices([{"id": planning_id, "week_start": planning.get("week_start", ""),
                             "data_json": planning.get("data_json") or "{}"}])[0]
    return {"success": True, "planning_id": planning_id, "url": f"/?draft={planning_id}", "draft": draft}
