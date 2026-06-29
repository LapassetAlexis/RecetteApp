"""Petites fonctions de nettoyage de texte partagées (app + extraction)."""

import re

# Marqueurs typiques d'un suffixe « source » à retirer du titre d'une recette.
_SOURCE_MARKERS = (
    "cooking", "recette", "cuisine", "marmiton", "750g",
    "ricardo", "ptitchef", "journaldesfemmes", "blog", ".com", ".fr",
    "minceur", "weight watchers",
)


def clean_recipe_title(name: str) -> str:
    """Retire les suffixes de site/source d'un titre de recette.

    Exemples :
      "Flans ... - Amandine Cooking"      -> "Flans ..."
      "Wrap ... | Recette Minceur"        -> "Wrap ..."
      "Wok ... - Recettes légères"        -> "Wok ..."
      "Pâtes ... WW"                      -> "Pâtes ..."
    Ne touche pas aux titres dont le segment après " - " n'est pas une source
    (ex. "Boeuf - carottes" reste inchangé).
    """
    if not name:
        return name
    s = name.strip()
    # "Titre | Site" -> on garde la partie gauche
    if " | " in s:
        s = s.split(" | ", 1)[0].strip()
    # "Titre - Source" -> couper si le segment de droite ressemble à une source
    if " - " in s:
        left, _, right = s.rpartition(" - ")
        if left and any(m in right.lower() for m in _SOURCE_MARKERS):
            s = left.strip()
    # Suffixe " WW" (Weight Watchers)
    s = re.sub(r"\s+WW$", "", s)
    return s.strip()


_UNITS = {
    "g", "kg", "mg", "ml", "cl", "dl", "l", "cs", "cc", "càs", "càc",
    "pincée", "pincées", "gousse", "gousses", "tranche", "tranches",
    "sachet", "sachets", "boîte", "boîtes", "pièce", "pièces", "verre", "verres",
    "tasse", "tasses", "botte", "bottes", "branche", "branches", "feuille",
    "feuilles", "brin", "brins", "cuillère", "cuillères", "cuillere", "cuilleres",
}


def parse_ingredient_line(line: str) -> dict | None:
    """Transforme une ligne texte en {nom, quantite, unite}. Tolérant.

    "200 g de farine" -> {nom:"farine", quantite:"200", unite:"g"}
    "3 oignons"       -> {nom:"oignons", quantite:"3", unite:""}
    "farine : 200 g"  -> {nom:"farine", quantite:"200", unite:"g"}
    "sel"             -> {nom:"sel", quantite:"", unite:""}
    """
    s = line.strip().lstrip("-•*–").strip()
    if not s:
        return None
    # format "nom : quantité unité"
    if ":" in s:
        nom, _, rest = s.partition(":")
        nom, rest = nom.strip(), rest.strip()
        m = re.match(r"^([\d.,/]+)\s*(.*)$", rest)
        if m:
            return {"nom": nom, "quantite": m.group(1).replace(",", "."), "unite": m.group(2).strip()}
        return {"nom": nom, "quantite": "", "unite": rest}
    # format "quantité unité nom"
    m = re.match(r"^([\d.,/]+)\s+(.*)$", s)
    if not m:
        return {"nom": s, "quantite": "", "unite": ""}
    qty = m.group(1).replace(",", ".")
    rest = m.group(2).strip()
    parts = rest.split(None, 1)
    if parts and parts[0].lower().rstrip(".") in _UNITS:
        unite = parts[0]
        nom = parts[1] if len(parts) > 1 else ""
        nom = re.sub(r"^(de\s+|d')", "", nom, flags=re.IGNORECASE).strip()
        return {"nom": nom, "quantite": qty, "unite": unite}
    return {"nom": rest, "quantite": qty, "unite": ""}


def split_instructions(text: str) -> list[str]:
    """Découpe des instructions en étapes.

    - plusieurs lignes -> une étape par ligne ;
    - un seul bloc -> découpage par phrases (. ! ?) suivies d'une majuscule/chiffre.
    """
    text = (text or "").strip()
    if not text:
        return []
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(lines) > 1:
        return lines
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-ZÀ-ÝÉÈ0-9])", text)
    return [s.strip() for s in sentences if s.strip()]


def _parse_qty(value) -> float | None:
    """Convertit une quantité en nombre. Gère décimales (1,5) et fractions (1/2).

    Renvoie None si non numérique (« une pincée », « QS »...) → non sommable.
    """
    s = str(value).strip().replace(",", ".")
    if not s:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", s)  # fraction
    if m:
        try:
            denom = float(m.group(2))
            return float(m.group(1)) / denom if denom else None
        except (ValueError, ZeroDivisionError):
            return None
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)
    return None


def _fmt_qty(n: float) -> str:
    """Formate une quantité numérique (entier sans .0, sinon 2 décimales max)."""
    if n == int(n):
        return str(int(n))
    return f"{round(n, 2):g}"


def merge_ingredients(items: list[dict]) -> list[dict]:
    """Fusionne une liste d'ingrédients par (nom, unité).

    - additionne les quantités numériques (décimales/fractions) de même unité ;
    - une même denrée en unités différentes reste sur 2 lignes (non additionnable) ;
    - quantités non numériques : on garde la première renseignée.
    Trie par nom.
    """
    out: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for ing in items:
        nom = (ing.get("nom") or "").strip()
        if not nom:
            continue
        unite = (ing.get("unite") or "").strip()
        qty = str(ing.get("quantite", "") or "").strip()
        key = (nom.lower(), unite.lower())
        if key not in out:
            out[key] = {"nom": nom, "quantite": qty, "unite": unite}
            order.append(key)
            continue
        existing = out[key]
        a, b = _parse_qty(existing["quantite"]), _parse_qty(qty)
        if a is not None and b is not None:
            existing["quantite"] = _fmt_qty(a + b)
        elif not existing["quantite"] and qty:
            existing["quantite"] = qty
        # sinon : non sommable → on conserve la première valeur
    return sorted((out[k] for k in order), key=lambda x: x["nom"].lower())
