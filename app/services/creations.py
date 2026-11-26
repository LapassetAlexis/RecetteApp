"""Repas libre et brique créés depuis la page planning (POST /api/free-meal,
POST /api/brique).

Une création = UNE recette Notion, placée dans un ou plusieurs créneaux (case
fusionnée, sélection multiple) en une seule écriture du planning. Les créneaux
sont vérifiés AVANT la création Notion : si l'un manque, rien n'est créé ni
écrit. La création Notion se fait hors verrou ; seules la relecture, la
mutation et l'écriture du planning sont sous le verrou du planning. Si une
erreur survient après la création, la page Notion est archivée et le planning
n'est pas modifié."""

import json
import logging

from app.deps import db, notion
from app.services.planning import (
    _brique_ingredients,
    _parse_slots,
    _place_brique,
    _set_main,
)
from app.services.shopping import _ingredients_to_text, _refresh_shopping_and_save
from app.text_utils import parse_ingredient_line

logger = logging.getLogger(__name__)
_MANQUANT = "Repas non trouvé dans le planning"


def slots_from_body(body: dict) -> list[tuple[int, str]] | None:
    """Créneaux visés : `slots` [{jour, moment}, …] (plusieurs jours d'une case
    fusionnée) ou, pour la compatibilité, `jour` + `moment`. None si invalide."""
    if "slots" in body:
        parsed = _parse_slots(body.get("slots"))
        return None if parsed is None else [(s["jour"], s["moment"]) for s in parsed]
    jour, moment = body.get("jour"), body.get("moment")
    if isinstance(jour, bool) or jour not in range(1, 8) or moment not in ("midi", "soir"):
        return None
    return [(jour, moment)]


def _cibles(data: dict, slots: list[tuple[int, str]]) -> list[dict] | None:
    """Repas visés dans `data["plats"]`, ou None si l'un manque."""
    by_slot = {(p.get("jour"), p.get("moment")): p for p in data.get("plats") or []}
    cibles = [by_slot.get(s) for s in slots]
    return cibles if all(cibles) else None


async def _check_slots(planning_id: int, slots: list[tuple[int, str]]) -> str | None:
    """Vérifie, avant toute création Notion, que le planning et les repas
    visés existent. Renvoie un message d'erreur, ou None."""
    planning = await db.get_planning_with_recipes(planning_id)
    if not planning:
        return "Planning introuvable"
    try:
        data = json.loads(planning["data_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        data = {}
    return None if _cibles(data, slots) else _MANQUANT


async def _create_and_place(planning_id: int, slots, create, place) -> dict:
    """Vérifie les créneaux, crée la recette dans Notion HORS verrou (`create`
    → (page_id, url) ou message d'erreur), puis, sous le verrou du planning,
    relit le planning, place la recette dans chaque cible
    (`place(cible, page_id, url)`) et écrit UNE fois. Si l'écriture échoue
    (créneau retiré entre-temps, exception…), la page Notion est archivée (pas
    de recette orpheline)."""
    erreur = await _check_slots(planning_id, slots)
    if erreur:
        return {"error": erreur}
    created = await create()
    if isinstance(created, str):
        return {"error": created}
    page_id, url = created

    def mutate(_planning: dict, data: dict) -> str | None:
        cibles = _cibles(data, slots)
        if cibles is None:
            return _MANQUANT
        for cible in cibles:
            place(cible, page_id, url)
        return None

    try:
        res = await _refresh_shopping_and_save(planning_id, mutate)
    except Exception:
        await _archive(page_id)
        raise
    if res.get("error"):
        await _archive(page_id)
    return res


async def _archive(page_id: str) -> None:
    if not page_id:
        return
    try:
        await notion.archive_recipe(page_id)
    except Exception:
        logger.exception("Archivage de la recette %s impossible", page_id)


async def place_free_meal(planning_id: int, body: dict) -> dict:
    """Crée une recette minimale « repas libre » (nom + ingrédients, une ligne
    par ingrédient) et la place comme plat dans le(s) créneau(x)."""
    slots = slots_from_body(body)
    nom = (body.get("nom") or "").strip()
    if not slots or not nom:
        return {"error": "Paramètres invalides (jour 1-7, moment midi/soir, nom)"}
    ingredients_text = body.get("ingredients") or ""
    structured = [
        i for i in (parse_ingredient_line(line) for line in ingredients_text.split("\n"))
        if i and i["nom"]
    ]

    async def create():
        result = await notion.create_recipe(nom=nom, repas="Plat")
        page_id = result.get("id", "")
        if not page_id:
            return "Création de la recette impossible"
        # Ingrédients : Notion + cache local structuré (mise à l'échelle,
        # nutrition, liste de courses).
        if structured:
            await notion.update_ingredients(page_id, _ingredients_to_text(structured))
            await db.save_enriched(page_id, nom, ingredients=json.dumps(structured))
        return page_id, result.get("url", "")

    def place(cible, page_id, url):
        # Nouveau plat principal : accompagnements réinitialisés ; vraie recette
        # Notion créée → plus un « repas libre ».
        _set_main(cible, nom, {"id": page_id, "url": "", "notion_url": url,
                               "repas": ["Plat"], "tags": []})
        cible["libre"] = False

    return await _create_and_place(planning_id, slots, create, place)


async def place_brique(planning_id: int, body: dict) -> dict:
    """Crée une brique (Nature=Ingrédient + Base, ingrédient de courses =
    elle-même) et la place en plat ou en accompagnement du/des créneau(x)."""
    slots = slots_from_body(body)
    if not slots:
        return {"error": "Paramètres invalides (jour 1-7, moment midi/soir)"}
    slot = body.get("slot")
    nom = (body.get("nom") or "").strip()
    if slot not in ("plat", "accompagnement") or not nom:
        return {"error": "Paramètres invalides (slot plat/accompagnement, nom)"}
    base = body.get("base") or []
    if isinstance(base, str):
        base = [base]
    base = [b for b in base if b]
    quantite = str(body.get("quantite") or "").strip()
    unite = str(body.get("unite") or "").strip()

    async def create():
        result = await notion.create_recipe(nom=nom, nature="Ingrédient", base=base)
        page_id = result.get("id", "")
        if not page_id:
            return "Création de la brique impossible"
        structured = _brique_ingredients(nom, quantite, unite)
        await notion.update_ingredients(page_id, _ingredients_to_text(structured))
        await db.save_enriched(page_id, nom, ingredients=json.dumps(structured))
        return page_id, result.get("url", "")

    def place(cible, page_id, url):
        _place_brique(cible, slot, nom, page_id, url, base)

    return await _create_and_place(planning_id, slots, create, place)
