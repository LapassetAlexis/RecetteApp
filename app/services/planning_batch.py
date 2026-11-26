"""Édition des repas d'un planning depuis la page planning : édition groupée
(POST /api/planning/{id}/meals : plat principal, accompagnements, sur un ou
plusieurs créneaux) et placement des briques créées à la volée."""

from app.config import recipe_base, recipe_nature, recipe_types
from app.deps import notion
from app.services.planning import (
    build_plat,
    index_by_name,
    nb_jours_periode,
    per_day_list,
    plat_accompagnements,
    set_main,
)
from app.text_utils import parse_ingredient_line

MEAL_ACTIONS = ("set_main", "add_side", "remove_side")


def parse_slots(raw) -> list[dict] | None:
    """Valide la liste `slots` du corps : [{jour 1-7, moment midi|soir,
    persons (optionnel, > 0)}]. Dédoublonne (jour, moment). None si vide ou
    si une entrée est invalide (on refuse tout plutôt que d'en ignorer)."""
    if not isinstance(raw, list) or not raw or len(raw) > 14:
        return None
    out: list[dict] = []
    seen: set[tuple[int, str]] = set()
    for s in raw:
        if not isinstance(s, dict):
            return None
        jour, moment = s.get("jour"), s.get("moment")
        if isinstance(jour, bool) or not isinstance(jour, int) or not 1 <= jour <= 7:
            return None
        if moment not in ("midi", "soir"):
            return None
        if (jour, moment) in seen:
            continue
        seen.add((jour, moment))
        try:
            persons = int(s.get("persons") or 0)
        except (TypeError, ValueError):
            persons = 0
        out.append({"jour": jour, "moment": moment, "persons": max(0, persons)})
    return out


def _new_plat(planning: dict, data: dict, slot: dict, nom: str,
              recette: dict | None) -> dict:
    """Crée le plat d'un créneau encore vide (cas de l'ajout depuis une fiche
    recette) : convives du slot, sinon du jour (`per_day`), sinon du planning."""
    persons = slot["persons"] or per_day_list(planning, data)[slot["jour"] - 1]
    item = {"nom": nom, "notion_id": recette["id"] if recette else "",
            "nature": recipe_nature(recette) if recette else "Recette",
            "libre": not recette}
    by_id = {recette["id"]: recette} if recette else {}
    return build_plat(item, slot["jour"], slot["moment"], persons, by_id)


async def resolve_side(nom: str) -> dict:
    """Construit le dict accompagnement pour un nom de recette, en reliant à
    Notion si la recette y existe (sinon accompagnement « libre » sans id)."""
    acc = {"nom_recette": nom, "notion_id": "", "url": "", "notion_url": "",
           # Nom inconnu au catalogue → accompagnement LIBRE (rappel courses).
           "libre": True}
    r = index_by_name(await notion.get_all_recipes()).get(nom.lower().strip())
    if r:
        acc.update(libre=False, notion_id=r["id"], url=r.get("url", ""),
                   notion_url=r.get("notion_url", ""))
    return acc


async def resolve_meal_action(action: str, nom: str) -> dict | None:
    """Ce que l'action place, lu dans Notion AVANT d'écrire (hors verrou) :
    la recette du catalogue pour set_main (None : repas libre), l'accompagnement
    résolu pour add_side, rien pour remove_side."""
    if action == "set_main":
        return index_by_name(await notion.get_all_recipes()).get(nom.lower().strip())
    if action == "add_side":
        return await resolve_side(nom)
    return None


def apply_meals_batch(planning: dict, data: dict, slots: list[dict], action: str,
                      nom: str, remplace: str = "", resolved: dict | None = None) -> str | None:
    """Applique UNE action à plusieurs créneaux, EN MÉMOIRE (`data["plats"]`).

    `resolved` : résultat de `resolve_meal_action` (recette ou accompagnement).

    Renvoie un message d'erreur (rien n'est persisté : l'appelant n'écrit pas)
    ou None si toutes les mutations ont réussi ; l'appelant régénère alors la
    liste de courses et écrit une seule fois.

    - set_main : remplace le plat principal (accompagnements remis à zéro) ;
      un créneau VIDE (ou « absent ») reçoit un nouveau plat ;
    - add_side : ajoute l'accompagnement `nom` (doublon ignoré) ; si `remplace`
      est fourni, retire d'abord cet accompagnement (« ↻ Changer ») ;
    - remove_side : retire l'accompagnement `nom`.
    Les actions d'accompagnement exigent un plat existant dans chaque créneau."""
    plats = data.setdefault("plats", [])
    by_slot = {(p.get("jour"), p.get("moment")): p for p in plats}
    if action != "set_main" and any((s["jour"], s["moment"]) not in by_slot for s in slots):
        return "Repas non trouvé dans le planning"

    if action == "set_main":
        recette = resolved
        off = set(data.get("off_meals") or [])
        for s in slots:
            target = by_slot.get((s["jour"], s["moment"]))
            if target is None:
                plats.append(_new_plat(planning, data, s, nom, recette))
                off.discard(f"{s['jour']}:{s['moment']}")
                continue
            set_main(target, nom, recette)
            target["libre"] = not bool(recette)
            if s["persons"]:
                target["persons"] = s["persons"]
        plats.sort(key=lambda p: (p.get("jour", 0), p.get("moment") != "midi"))
        if "off_meals" in data:
            data["off_meals"] = sorted(off)
        if data.get("nb_jours"):
            data["nb_jours"] = nb_jours_periode(data)
        return None

    cle = nom.lower().strip()
    ancien = remplace.lower().strip()
    side = resolved if action == "add_side" else None
    for s in slots:
        target = by_slot[(s["jour"], s["moment"])]
        accs = plat_accompagnements(target)
        retire = cle if action == "remove_side" else ancien
        if retire:
            accs = [a for a in accs if a.get("nom_recette", "").lower().strip() != retire]
        if side and not any(a.get("nom_recette", "").lower().strip() == cle for a in accs):
            accs.append(dict(side))
        target["accompagnements"] = accs
        target.pop("accompagnement", None)
    return None


# ── Briques (composants simples créés depuis la page planning) ──────────


def brique_ingredients(nom: str, quantite: str, unite: str) -> list[dict]:
    """L'ingrédient de courses d'une brique est elle-même. On normalise via
    parse_ingredient_line pour découper qté/unité proprement, avec repli sur
    les valeurs saisies si le parsing ne renvoie rien d'exploitable."""
    parsed = parse_ingredient_line(" ".join(p for p in (quantite, unite, nom) if p))
    if parsed and parsed.get("nom"):
        return [parsed]
    return [{"nom": nom, "quantite": quantite, "unite": unite}]


def place_brique_repas(target: dict, slot: str, nom: str, page_id: str,
                       notion_url: str, base: list[str]) -> None:
    """Place une brique fraîchement créée dans le repas `target`, en plat
    (elle remplace le plat, comme /api/free-meal) ou en accompagnement (ajout
    en fin de liste, sans écraser les autres)."""
    if slot == "plat":
        set_main(target, nom, {
            "id": page_id,
            "url": "",
            "notion_url": notion_url,
            "nature": "Ingrédient",
            "base": base,
            "repas": [],
            "tags": [],
        })
        return
    accs = plat_accompagnements(target)
    accs.append({
        "nom_recette": nom,
        "notion_id": page_id,
        "url": "",
        "notion_url": notion_url,
        "nature": "Ingrédient",
        "base": base,
    })
    target["accompagnements"] = accs
    target.pop("accompagnement", None)


# ── Recettes proposées en remplacement (sélecteur de la page planning) ──


def alternatives(recettes: list[dict], used_names: set[str]) -> list[dict]:
    """Recettes proposables en remplacement, hors celles déjà au planning.

    On expose la Nature pour que le picker distingue les recettes des
    « briques » (composants simples). On garde aussi les briques
    (Nature=Ingrédient) : réutilisables en plat (n'importe quelle Base) comme
    en accompagnement (Base=Légume)."""
    return [
        {"nom": r["nom"], "repas": recipe_types(r), "base": recipe_base(r),
         "tags": r["tags"], "nature": recipe_nature(r)}
        for r in recettes
        if r["nom"] not in used_names
        and (
            recipe_nature(r) == "Ingrédient"
            or not recipe_types(r)
            or set(recipe_types(r)) & {"Plat", "Entrée"}
            or "Légume" in recipe_base(r)
        )
    ]
