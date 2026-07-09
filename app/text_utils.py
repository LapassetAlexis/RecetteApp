"""Petites fonctions de nettoyage de texte partagées (app + extraction)."""

import re
import unicodedata

# Marqueurs typiques d'un suffixe « source » à retirer du titre d'une recette.
_SOURCE_MARKERS = (
    "cooking", "recette", "cuisine", "marmiton", "750g",
    "ricardo", "ptitchef", "journaldesfemmes", "blog", ".com", ".fr",
    "minceur", "weight watchers",
)


def normalize_title_case(s: str) -> str:
    """Normalise la casse d'un titre crié en MAJUSCULES en casse de phrase.

    Ne touche qu'aux titres SANS aucune minuscule (ex. titres importés tout en
    capitales). Les titres déjà en casse mixte sont laissés intacts pour
    préserver les noms propres.

      "WRAP D'ÉPINARDS FROMAGE FOUETTÉ ET JAMBON" -> "Wrap d'épinards fromage fouetté et jambon"
      "Steak haché, ratatouille et semoule"       -> inchangé
    """
    if not s:
        return s
    letters = [c for c in s if c.isalpha()]
    if not letters or any(c.islower() for c in letters):
        return s
    s = s.lower()
    for i, c in enumerate(s):
        if c.isalpha():
            return s[:i] + c.upper() + s[i + 1:]
    return s


def clean_recipe_title(name: str) -> str:
    """Retire les suffixes de site/source d'un titre de recette et normalise la casse.

    Exemples :
      "Flans ... - Amandine Cooking"      -> "Flans ..."
      "Wrap ... | Recette Minceur"        -> "Wrap ..."
      "Wok ... - Recettes légères"        -> "Wok ..."
      "Pâtes ... WW"                      -> "Pâtes ..."
      "WRAP D'ÉPINARDS ET JAMBON"         -> "Wrap d'épinards et jambon"
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
    return normalize_title_case(s.strip())


# Repli d'accents 1:1 (préserve les indices, contrairement à NFD) pour
# reconnaître les unités quelle que soit la casse/accentuation.
_ACCENT_FOLD = str.maketrans(
    "àâäáãéèêëíìîïóòôöõúùûüçñ",
    "aaaaaeeeeiiiiooooouuuucn",
)

# Unités canoniques ← variantes. Ordre = longueur décroissante (le regex teste
# les formes longues avant les courtes). L'unité doit être suivie d'un espace ou
# d'une fin (lookahead) pour ne pas manger un mot (« lardons », « cassonade »...).
_UNIT_PATTERNS: list[tuple[str, str]] = [
    ("c. à s.", r"cuilleres?\s+a\s+soupe|cuil\.?\s+a\s+soupe|c\.?\s*a\.?\s*s\.?|cas|cs"),
    ("c. à c.", r"cuilleres?\s+a\s+cafe|cuil\.?\s+a\s+cafe|c\.?\s*a\.?\s*c\.?|cac|cc"),
    ("kg", r"kilogrammes?|kilos?|kg"),
    ("mg", r"mg"),
    ("g", r"grammes?|grs?|g"),
    ("cl", r"cl"),
    ("ml", r"ml"),
    ("l", r"litres?|l"),
    ("pincée", r"pincees?"),
    ("gousse", r"gousses?"),
    ("tranche", r"tranches?"),
    ("feuille", r"feuilles?"),
    ("sachet", r"sachets?"),
    ("boîte", r"boites?"),
    ("pièce", r"pieces?"),
    ("verre", r"verres?"),
    ("brin", r"brins?"),
    ("botte", r"bottes?"),
    ("bouquet", r"bouquets?"),
    ("poignée", r"poignees?"),
    ("pot", r"pots?"),
    ("boule", r"boules?"),
]
# Un regex par unité (testés dans l'ordre = longues formes d'abord).
_UNIT_LOOKUP = [(re.compile(r"^(?:" + p + r")(?=\s|$)", re.IGNORECASE), canon)
                for canon, p in _UNIT_PATTERNS]

# Mots de liaison à retirer en tête du nom après l'unité (« 700 g DE farine »).
_CONNECTOR_RE = re.compile(r"^(?:de\s+|d['’]\s*|des\s+|du\s+)", re.IGNORECASE)


def _match_unit(rest: str) -> tuple[str, str]:
    """(unité canonique, nom restant) si `rest` commence par une unité connue,
    sinon ("", rest). Insensible casse/accents, préserve la casse du nom."""
    folded = rest.lower().translate(_ACCENT_FOLD)
    for rx, canon in _UNIT_LOOKUP:
        m = rx.match(folded)
        if m:
            name = rest[m.end():].lstrip()
            name = _CONNECTOR_RE.sub("", name).strip()
            if name:  # sinon l'« unité » était en fait le nom (ex. « Litre »)
                return canon, name
    return "", rest


def parse_ingredient_line(line: str) -> dict | None:
    """Transforme une ligne en {nom, quantite, unite} NORMALISÉ (en amont).

    Extrait la quantité de tête, reconnaît l'unité (dictionnaire canonique,
    insensible casse/accents) et retire le mot de liaison « de/d'/du ». Ce qui
    reste est le nom propre — plus d'unité qui fuit dans le libellé.

    "700 g d'épinards"                 -> {nom:"épinards", quantite:"700", unite:"g"}
    "1/2 cuillère à café de fond ..."  -> {nom:"fond ...", quantite:"1/2", unite:"c. à c."}
    "Cs huile"                         -> {nom:"huile", quantite:"", unite:"c. à s."}
    "4 œufs"                           -> {nom:"œufs", quantite:"4", unite:""}
    "Sel, poivre"                      -> {nom:"Sel, poivre", quantite:"", unite:""}
    "farine : 200 g" (legacy)          -> {nom:"farine", quantite:"200", unite:"g"}
    """
    s = line.strip().lstrip("-•*–").strip()
    if not s:
        return None
    # Ancien format interne "nom : quantité unité"
    if " : " in s:
        nom, _, rest = s.partition(" : ")
        m = re.match(r"^([\d.,/]+)\s*(.*)$", rest.strip())
        if m:
            unite, _ = _match_unit(m.group(2).strip()) if m.group(2).strip() else ("", "")
            return {"nom": nom.strip(), "quantite": m.group(1).replace(",", "."),
                    "unite": unite or m.group(2).strip()}
        return {"nom": nom.strip(), "quantite": "", "unite": ""}
    # Format source "quantité unité nom". \s* pour gérer "200g de pâtes".
    m = re.match(r"^([\d]+(?:[.,/]\d+)?)\s*(.*)$", s)
    if m and m.group(2).strip():
        qty, rest = m.group(1).replace(",", "."), m.group(2).strip()
    else:
        qty, rest = "", s
    unite, nom = _match_unit(rest)
    return {"nom": nom, "quantite": qty, "unite": unite}


# Séparateurs de listes de condiments (« sel, poivre », « sel et poivre »).
_CONDIMENT_SPLIT_RE = re.compile(r"\s*(?:,|\+|&|\bet\b)\s*", re.IGNORECASE)


def _split_condiments(nom: str) -> list[str]:
    """Éclate une ligne d'assaisonnements en items simples.

    « Sel, poivre » -> ["Sel", "poivre"] ; « Sel et poivre » -> [...]. Ne
    s'applique que si aucun chiffre et si chaque morceau reste court (≤3 mots),
    pour ne pas casser « poulet à la crème et estragon » ni « farine, tamisée »
    quantifiée.
    """
    if any(c.isdigit() for c in nom):
        return [nom]
    parts = [p.strip(" .").strip() for p in _CONDIMENT_SPLIT_RE.split(nom)]
    parts = [p for p in parts if p]
    if len(parts) >= 2 and all(len(p.split()) <= 3 for p in parts):
        return parts
    return [nom]


def normalize_cached_ingredient(ing: dict) -> list[dict]:
    """Re-normalise un ingrédient (possiblement issu d'un ancien cache « sale »)
    et éclate les listes de condiments. Idempotent sur une entrée déjà propre.

    Reconstruit la ligne « qty unite nom » puis la re-parse (corrige les unités
    qui avaient fui dans le nom), puis éclate si ni quantité ni unité."""
    line = " ".join(p for p in (
        str(ing.get("quantite", "") or "").strip(),
        (ing.get("unite", "") or "").strip(),
        (ing.get("nom", "") or "").strip(),
    ) if p)
    parsed = parse_ingredient_line(line)
    if not parsed or not parsed["nom"]:
        return []
    if not parsed["quantite"] and not parsed["unite"]:
        parts = _split_condiments(parsed["nom"])
        if len(parts) > 1:
            return [{"nom": p, "quantite": "", "unite": ""} for p in parts]
    return [parsed]


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


def _normalize_form(s: str) -> str:
    """Normalise la FORME d'un libellé pour le regroupement (pas le sens).

    Minuscule, sans accents, espaces compactés, singulier/pluriel replié
    (retrait d'un s/x final par mot de >3 lettres). Regroupe « Oignons » et
    « oignon », mais PAS « oignon rouge » et « oignon jaune » (qualificatif
    différent), ni « tomate » et « tomate cerise ».
    """
    s = unicodedata.normalize("NFD", s.strip().lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")  # retire accents
    s = re.sub(r"\s+", " ", s)
    words = [re.sub(r"(?<=\w{3})[sx]$", "", w) for w in s.split(" ")]
    return " ".join(words)


def scale_ingredients(items: list[dict], factor: float) -> list[dict]:
    """Met à l'échelle les quantités numériques par `factor` (nb_pers / base).

    Les quantités non numériques (« une pincée », « QS ») sont laissées telles
    quelles. Retourne de NOUVEAUX dicts (n'altère pas l'entrée).
    """
    out: list[dict] = []
    for ing in items:
        new = dict(ing)
        n = _parse_qty(ing.get("quantite", ""))
        if n is not None and factor and factor > 0:
            new["quantite"] = _fmt_qty(n * factor)
        out.append(new)
    return out


def _ingredient_sources(ing: dict) -> list[str]:
    """Titres de recettes source portés par un ingrédient (`recettes` ou `recette`)."""
    recs = ing.get("recettes")
    if recs:
        return [s for s in recs if s]
    one = ing.get("recette")
    return [one] if one else []


def merge_ingredients(items: list[dict]) -> list[dict]:
    """Fusionne une liste d'ingrédients par (nom, unité).

    - additionne les quantités numériques (décimales/fractions) de même unité ;
    - une même denrée en unités différentes reste sur 2 lignes (non additionnable) ;
    - quantités non numériques : on garde la première renseignée ;
    - union des recettes source (champ `recettes`), dans l'ordre de rencontre.
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
        srcs = _ingredient_sources(ing)
        # clé normalisée (forme) : regroupe singulier/pluriel + casse/accents,
        # mais conserve distinctes les variantes (oignon rouge / jaune).
        key = (_normalize_form(nom), _normalize_form(unite))
        if key not in out:
            out[key] = {
                "nom": nom, "quantite": qty, "unite": unite,
                "recettes": list(dict.fromkeys(srcs)),
            }
            order.append(key)
            continue
        existing = out[key]
        a, b = _parse_qty(existing["quantite"]), _parse_qty(qty)
        if a is not None and b is not None:
            existing["quantite"] = _fmt_qty(a + b)
        elif not existing["quantite"] and qty:
            existing["quantite"] = qty
        # sinon : non sommable → on conserve la première valeur
        for s in srcs:
            if s not in existing["recettes"]:
                existing["recettes"].append(s)
    return sorted((out[k] for k in order), key=lambda x: x["nom"].lower())
