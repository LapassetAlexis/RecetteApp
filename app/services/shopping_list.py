"""Liste de courses partagée d'un planning : clés d'article, rangement par
rayon, coches, articles ajoutés à la main, articles masqués, placard du foyer,
vue affichable et jeton de partage public. Les écritures passent par
`store.update_planning` (verrou du planning)."""

import json
import re
import secrets
import zlib

from app.categories import RAYON_ORDER, categorize, group_by_rayon
from app.deps import db
from app.services import store
from app.text_utils import _normalize_form, _parse_qty, _unit_info, merge_ingredients, parse_ingredient_line

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
    """Article manuel depuis une saisie rapide (« 2 l lait », « café »).

    Les parenthèses sont gardées (« Papier toilette (x6) »). Un nom sans
    aucune lettre (« 2 », « 3,5 ») est refusé : None."""
    texte = str(texte or "").strip()[:_TEXTE_MAX]
    parsed = parse_ingredient_line(texte, keep_parens=True) if texte else None
    if not parsed or not any(c.isalpha() for c in parsed.get("nom", "")):
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


def set_extra_rayon(data: dict, key: str, rayon: str) -> bool:
    """Range un article manuel dans le rayon choisi (un des RAYON_ORDER)."""
    if rayon not in RAYON_ORDER:
        return False
    extras = _extras(data)
    item = next((e for e in extras if e.get("key") == key), None)
    if item is None:
        return False
    item["rayon"] = rayon
    data["courses_extra"] = extras
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


async def set_placard(nom: str, present: bool) -> bool:
    """Ajoute ou retire un nom du placard, sans toucher aux autres réglages.
    Renvoie True si le placard a changé."""
    nom = _normalize_form(nom)
    if not nom:
        return False
    # Verrou des réglages : deux ajouts simultanés ne s'écrasent pas.
    async with store.prefs_lock():
        prefs = await db.get_app_prefs()
        placard = set(await get_placard())
        if (nom in placard) == present:
            return False
        if present:
            placard.add(nom)
        else:
            placard.discard(nom)
        prefs["placard"] = sorted(placard)
        await db.save_app_prefs(prefs)
        return True


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
        # Page planning : lien de partage actif, taille du placard, rayons
        # proposés pour un article manuel.
        "partage": bool(data.get("share_token")),
        "nb_placard": len(placard or []),
        "rayons": RAYON_ORDER,
    }


# ── Partage public : jeton révocable (D8) ───────────────────────────────

SHARE_TOKEN_BYTES = 16      # nouveaux liens ; les anciens (8 octets) restent valides


def share_action(data: dict, action: str = "") -> str | None:
    """Applique une action de partage à `data` (modifié en place) et renvoie le
    jeton actif, ou None s'il n'y en a plus.

    - "" : jeton existant, sinon nouveau ;
    - "revoquer" : plus de jeton (le lien répond 404) ;
    - "regenerer" : nouveau jeton (l'ancien lien répond 404)."""
    if action == "revoquer":
        data.pop("share_token", None)
        return None
    if action == "regenerer" or not data.get("share_token"):
        data["share_token"] = secrets.token_urlsafe(SHARE_TOKEN_BYTES)
    return data["share_token"]


async def load_shopping_view(data: dict) -> dict:
    """shopping_view avec le placard du foyer."""
    return shopping_view(data, await get_placard())


async def mutate_planning_data(planning_id: int, fn):
    """Lit le data_json d'un planning, applique `fn(data)` puis enregistre si `fn`
    renvoie une valeur vraie, sous le verrou du planning (`store.update_planning`) :
    deux coches simultanées (page publique + page planning) ne s'écrasent pas.

    Renvoie (data, résultat de fn), ou (None, None) si le planning n'existe pas."""
    def _fn(_planning: dict, data: dict):
        res = fn(data)
        return res if res else store.NoWrite(res)

    return await store.update_planning(planning_id, _fn)
