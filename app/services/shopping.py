"""Liste de courses d'un planning : collecte des ingrédients en cache, mise à
l'échelle, fusion, rangement par rayon et enregistrement."""

import asyncio
import json
import logging
import re
import zlib

from app.categories import RAYON_ORDER, categorize, group_by_rayon
from app.deps import BASE_SERVINGS, db
from app.services.planning import (
    _builder_from_plats,
    _load_slot,
    _per_day_list,
    _recipe_rows,
    plat_accompagnements,
)
from app.text_utils import (
    _normalize_form,
    _parse_qty,
    _unit_info,
    clean_recipe_title,
    merge_ingredients,
    normalize_cached_ingredient,
    parse_ingredient_line,
    scale_ingredients,
)

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
    finalize_items(liste_courses)
    planning_data["liste_courses"] = liste_courses
    # La grille du constructeur suit les plats : un brouillon modifié depuis
    # la page planning puis « Repris » garde les modifications.
    planning_data["builder"] = _builder_from_plats(
        plats, planning_data.get("off_meals"), planning.get("nb_personnes") or 4,
    )
    # Coches et masques orphelins : on ne garde que ceux d'un article encore présent.
    purge_orphans(planning_data)

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


# ── Liste partagée : clés d'article, coches, articles manuels, placard ──
#
# Clés stockées dans planning_data (toutes optionnelles, repli sur vide) :
#   courses_checked : clés cochées. Une ancienne coche « nom seul » (sans « | »)
#                     coche toutes les lignes de ce nom (lecture tolérante) ;
#   courses_extra   : articles ajoutés à la main [{nom, quantite, unite, rayon, key}] ;
#   courses_masques : clés des articles de recette masqués (« Je l'ai déjà »).
# Le placard (articles toujours à la maison) est un réglage du foyer :
# app_prefs["placard"] = [nom normalisé].

EXTRA_PREFIX = "extra:"
_EXTRA_MAX = 200            # garde-fou : nombre d'articles manuels par planning
_TEXTE_MAX = 120            # longueur max d'une saisie rapide
_locks: dict[tuple[int, int], asyncio.Lock] = {}


def item_key(item: dict) -> str:
    """Clé stable d'un article : « nom normalisé|famille d'unité ».

    Même regroupement que merge_ingredients : masses et volumes forment une
    famille (« lait|volume »), les autres unités valent pour elles-mêmes
    (« oeuf| », « sel|pincee »). Deux lignes de même nom et d'unités
    incompatibles ont donc deux clés distinctes (B7)."""
    nom = _normalize_form(str(item.get("nom") or ""))
    unite = str(item.get("unite") or "")
    qty = str(item.get("quantite") or "").strip()
    info = _unit_info(unite)
    # « quelques g » : merge_ingredients ne convertit pas, il groupe par unité.
    if info is not None and qty and _parse_qty(qty) is None:
        info = None
    return f"{nom}|{info[0] if info else _normalize_form(unite)}"


def name_key(item: dict) -> str:
    """Nom normalisé d'un article (clé du placard, indépendante de l'unité)."""
    return _normalize_form(str(item.get("nom") or ""))


def finalize_items(items: list[dict]) -> list[dict]:
    """Range chaque article de la liste générée dans son rayon et lui écrit sa clé."""
    for it in items:
        it["rayon"] = categorize(it.get("nom", ""))
        it["key"] = item_key(it)
    return items


def fmt_quantite(q) -> str:
    """Quantité affichée avec la virgule décimale française (« 1.25 » → « 1,25 »).

    Le stockage garde le point (merge_ingredients, mise à l'échelle)."""
    return re.sub(r"(?<=\d)\.(?=\d)", ",", str(q or "").strip())


def _key_of(item: dict) -> str:
    return str(item.get("key") or item_key(item))


def _legacy_name(item: dict) -> str:
    """Forme des anciennes coches : le nom en minuscules."""
    return str(item.get("nom") or "").strip().lower()


def _recipe_items(data: dict) -> list[dict]:
    return [it for it in data.get("liste_courses") or [] if isinstance(it, dict) and it.get("nom")]


def _extras(data: dict) -> list[dict]:
    return [it for it in data.get("courses_extra") or [] if isinstance(it, dict) and it.get("nom")]


def _all_items(data: dict) -> list[dict]:
    return _recipe_items(data) + _extras(data)


def _checked_set(data: dict) -> set[str]:
    return {str(c).strip().lower() for c in data.get("courses_checked") or []}


def apply_check(data: dict, item: str, checked: bool, strict: bool = False) -> bool:
    """Coche / décoche un article dans `data` (modifié en place).

    `item` est une clé d'article. Hors mode strict, un nom seul (ancien client,
    file hors-ligne) est encore accepté et garde l'ancien comportement : il
    vaut pour toutes les lignes de ce nom. En mode strict (lien public), seule
    une clé d'article existante est acceptée. Renvoie False si refusé."""
    item = str(item or "").strip().lower()
    if not item:
        return False
    items = _all_items(data)
    cur = _checked_set(data)
    if item in {_key_of(it) for it in items}:
        if checked:
            cur.add(item)
        else:
            cur.discard(item)
            # Une ancienne coche par nom couvrait aussi cette ligne : on la
            # remplace par les clés des AUTRES lignes de ce nom (qui restent cochées).
            for name in {_legacy_name(it) for it in items if _key_of(it) == item} & cur:
                cur.discard(name)
                cur.update(_key_of(o) for o in items
                           if _legacy_name(o) == name and _key_of(o) != item)
    elif strict or "|" in item:
        return False
    elif checked:
        cur.add(item)
    else:
        cur.discard(item)
        cur.difference_update(_key_of(it) for it in items if _legacy_name(it) == item)
    data["courses_checked"] = sorted(cur)
    return True


def purge_orphans(data: dict) -> None:
    """Retire les coches et masques qui ne correspondent plus à aucun article (B23)."""
    items = _all_items(data)
    keys = {_key_of(it) for it in items}
    names = {_legacy_name(it) for it in items}
    if data.get("courses_checked"):
        data["courses_checked"] = sorted(c for c in _checked_set(data) if c in keys or c in names)
    if data.get("courses_masques"):
        data["courses_masques"] = [m for m in data["courses_masques"] if m in keys]


def reset_checks(data: dict) -> None:
    """Nouvelle liste générée : on repart sans coche, sauf pour les articles manuels."""
    data["courses_checked"] = sorted(c for c in _checked_set(data) if c.startswith(EXTRA_PREFIX))


def make_extra(texte: str) -> dict | None:
    """Article manuel depuis une saisie rapide (« 2 l lait », « café »)."""
    texte = str(texte or "").strip()[:_TEXTE_MAX]
    parsed = parse_ingredient_line(texte) if texte else None
    if not parsed or not parsed.get("nom", "").strip():
        return None
    it = {
        "nom": parsed["nom"].strip(),
        "quantite": parsed.get("quantite", ""),
        "unite": parsed.get("unite", ""),
        "rayon": categorize(parsed["nom"]),
    }
    it["key"] = EXTRA_PREFIX + item_key(it)
    return it


def add_extra(data: dict, texte: str) -> dict | None:
    """Ajoute un article manuel ; même article déjà saisi → quantités additionnées."""
    new = make_extra(texte)
    if new is None:
        return None
    extras = _extras(data)
    same = next((e for e in extras if e.get("key") == new["key"]), None)
    if same is not None:
        total = merge_ingredients([same, new])[0]
        same["quantite"], same["unite"] = total["quantite"], total["unite"]
        new = same
    elif len(extras) >= _EXTRA_MAX:
        return None
    else:
        extras.append(new)
    data["courses_extra"] = extras
    return new


def remove_extra(data: dict, key: str) -> bool:
    extras = _extras(data)
    kept = [e for e in extras if e.get("key") != key]
    if len(kept) == len(extras):
        return False
    data["courses_extra"] = kept
    data["courses_checked"] = sorted(_checked_set(data) - {key})
    return True


def set_masque(data: dict, key: str, masque: bool) -> bool:
    """Masque (« Je l'ai déjà ») ou restaure un article issu d'une recette."""
    if key not in {_key_of(it) for it in _recipe_items(data)}:
        return False
    masques = [m for m in data.get("courses_masques") or [] if m != key]
    if masque:
        masques.append(key)
    data["courses_masques"] = masques
    return True


def recipe_item_name(data: dict, key: str) -> str | None:
    """Nom normalisé de l'article de recette `key` (None s'il n'existe pas)."""
    return next((name_key(it) for it in _recipe_items(data) if _key_of(it) == key), None)


async def get_placard() -> list[str]:
    """Noms normalisés du placard (réglage du foyer), [] par défaut."""
    placard = (await db.get_app_prefs()).get("placard")
    if not isinstance(placard, list):
        return []
    return sorted({_normalize_form(str(p)) for p in placard if str(p).strip()})


async def set_placard(nom: str, present: bool) -> None:
    """Ajoute ou retire un nom du placard, sans toucher aux autres réglages."""
    nom = _normalize_form(nom)
    if not nom:
        return
    prefs = await db.get_app_prefs()
    placard = set(await get_placard())
    if present:
        placard.add(nom)
    else:
        placard.discard(nom)
    prefs["placard"] = sorted(placard)
    await db.save_app_prefs(prefs)


def _view_item(it: dict, checked: set[str], extra: bool) -> dict:
    key = _key_of(it)
    rayon = it.get("rayon") or categorize(it.get("nom", ""))
    qte = fmt_quantite(it.get("quantite"))
    return {
        **it,
        "key": key,
        "rayon": rayon if rayon in RAYON_ORDER else "Autre",
        "qte": f"{qte} {str(it.get('unite') or '').strip()}".strip() if qte else "",
        "checked": key in checked or _legacy_name(it) in checked,
        "extra": extra,
    }


def shopping_view(data: dict, placard: list[str] | None = None) -> dict:
    """État affichable de la liste de courses d'un planning.

    - `articles` : lignes visibles (recettes + articles manuels), triées par nom,
      chacune avec sa clé, sa quantité affichée (`qte`) et son état coché ;
    - `masques` : lignes de recette masquées, ou rangées d'office car au placard ;
    - `version` : empreinte des lignes visibles (change si la liste change, pas
      quand on coche) pour que la page publique sache qu'elle doit se recharger."""
    checked = _checked_set(data)
    masques = set(data.get("courses_masques") or [])
    placard_set = set(placard or [])
    visibles: list[dict] = []
    caches: list[dict] = []
    for it in _recipe_items(data):
        v = _view_item(it, checked, extra=False)
        if v["key"] in masques:
            caches.append({**v, "raison": "masque"})
        elif name_key(it) in placard_set:
            caches.append({**v, "raison": "placard"})
        else:
            visibles.append(v)
    visibles += [_view_item(e, checked, extra=True) for e in _extras(data)]
    visibles.sort(key=lambda v: _normalize_form(v["nom"]))
    for i, v in enumerate(visibles):
        v["ord"] = i
    pris = [v for v in visibles if v["checked"]]
    empreinte = json.dumps([[v["key"], v["qte"]] for v in visibles], ensure_ascii=False)
    return {
        "genere": bool(data.get("liste_courses")),
        "articles": visibles,
        "par_rayon": group_by_rayon(visibles),
        "pris": pris,
        "masques": caches,
        "checked": [v["key"] for v in pris],
        "nb_total": len(visibles),
        "nb_restants": len(visibles) - len(pris),
        "version": zlib.crc32(empreinte.encode("utf-8")),
    }


async def load_shopping_view(data: dict) -> dict:
    """shopping_view avec le placard du foyer."""
    return shopping_view(data, await get_placard())


async def mutate_planning_data(planning_id: int, fn):
    """Lit le data_json d'un planning, applique `fn(data)` puis enregistre si `fn`
    renvoie une valeur vraie. Verrou par planning : deux coches simultanées
    (page publique + page planning) ne s'écrasent pas.

    Renvoie (data, résultat de fn), ou (None, None) si le planning n'existe pas."""
    lock = _locks.setdefault((id(asyncio.get_running_loop()), planning_id), asyncio.Lock())
    async with lock:
        planning = await db.get_planning_with_recipes(planning_id)
        if not planning:
            return None, None
        data = json.loads(planning["data_json"])
        res = fn(data)
        if res:
            await db.update_planning_data(planning_id, json.dumps(data, ensure_ascii=False))
        return data, res
