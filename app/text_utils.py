"""Petites fonctions de nettoyage de texte partagées (app + extraction)."""

import math
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
    # « cuillère(s) / cuillerée(s) / cuil. / c. » + « à soupe / à s. » (et café).
    ("c. à s.", (r"(?:cuilleree?s?|cuil\.?|c\.?)\s*a\s+soupe"
                 r"|(?:cuil\.?|c\.?)\s*a\.?\s*s\.?|cas|cs")),
    ("c. à c.", (r"(?:cuilleree?s?|cuil\.?|c\.?)\s*a\s+cafe"
                 r"|(?:cuil\.?|c\.?)\s*a\.?\s*c\.?|cac|cc")),
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


# Mots de préparation à retirer du nom (n'importe où) — pour que « Ail haché »
# et « Ail » se regroupent. On garde les qualificatifs porteurs de SENS
# (frais, surgelé, rouge, fraîche…) : « crème fraîche » ≠ « crème ».
_PREP_WORDS = {
    "hache", "hachee", "hachees", "haches", "emince", "emincee", "emincees",
    "eminces", "rape", "rapee", "rapees", "rapes", "essore", "essoree",
    "essorees", "essores", "coupe", "coupee", "coupees", "coupes", "cisele",
    "ciselee", "ciselees", "ciseles", "cuit", "cuite", "cuites", "cuits",
    "fondu", "fondue", "fondus", "fondues", "egoutte", "egouttee", "egouttees",
    "egouttes", "prealablement",
}
_ARTICLE_RE = re.compile(r"^(?:un|une|le|la|les|des|du|de|d['’])\s+", re.IGNORECASE)


def _fold_word(w: str) -> str:
    """Minuscule sans accents ni ligature (pour comparer aux mots de préparation)."""
    w = w.lower().replace("œ", "oe").replace("æ", "ae")
    w = unicodedata.normalize("NFD", w)
    return "".join(c for c in w if unicodedata.category(c) != "Mn")


def _clean_ingredient_name(nom: str) -> str:
    """Nettoie un libellé d'ingrédient pour l'affichage ET le regroupement :
    retire parenthèses/commentaires, article de tête, « al dente » et les mots
    de préparation. Ne vide jamais le nom (repli sur l'original si besoin)."""
    s = (nom or "").strip()
    if not s:
        return s
    original = s
    s = re.sub(r"\([^)]*\)", " ", s)          # « Mozzarella (ou feta) » → « Mozzarella »
    s = s.replace("...", " ").replace("(", " ").replace(")", " ")
    s = re.sub(r"\bal dente\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip(" ,.;:")
    s = _ARTICLE_RE.sub("", s)                # « Un poireaux » → « poireaux »
    tokens = s.split()
    kept = [t for t in tokens if _fold_word(t) not in _PREP_WORDS]
    s = " ".join(kept).strip(" ,.;:")
    return s or original.strip()


# Fractions Unicode → (numérateur, dénominateur).
_UNICODE_FRACTIONS = {
    "½": (1, 2), "¼": (1, 4), "¾": (3, 4), "⅓": (1, 3), "⅔": (2, 3), "⅛": (1, 8),
}
_UF = "[" + "".join(_UNICODE_FRACTIONS) + "]"
_SIMPLE_NUM = r"\d+/\d+|\d+(?:[.,]\d+)?"
# Quantité de tête, du plus spécifique au plus simple :
#   intervalle « 2-3 », « 2 à 3 » (borne haute gardée) ; nombre mixte « 1 1/2 »,
#   « 1½ » ; fraction Unicode seule « ½ » ; nombre simple « 200 », « 1,5 », « 1/2 ».
_QTY_RANGE_RE = re.compile(
    rf"^(?:{_SIMPLE_NUM})(?:\s*[-–—]\s*|\s+(?:à|a)\s+)(?P<hi>{_SIMPLE_NUM})(?![\d/.,])")
_QTY_MIXED_RE = re.compile(r"^(?P<int>\d+)\s+(?P<num>\d+)/(?P<den>\d+)(?![\d/.,])")
_QTY_MIXED_UF_RE = re.compile(rf"^(?P<int>\d+)\s*(?P<uf>{_UF})")
_QTY_UF_RE = re.compile(rf"^(?P<uf>{_UF})")
_QTY_SIMPLE_RE = re.compile(rf"^(?P<n>{_SIMPLE_NUM})")


def _split_leading_qty(s: str) -> tuple[str, str]:
    """(quantité normalisée, reste) si `s` commence par une quantité, sinon ("", s).

    « 1 1/2 l » -> ("1.5", "l") ; « ½ citron » -> ("1/2", "citron") ;
    « 2-3 carottes » -> ("3", "carottes") ; « 1,5 kg » -> ("1.5", "kg").
    """
    m = _QTY_RANGE_RE.match(s)
    if m:
        return m.group("hi").replace(",", "."), s[m.end():].strip()
    m = _QTY_MIXED_RE.match(s)
    if m and int(m.group("den")):
        n = int(m.group("int")) + int(m.group("num")) / int(m.group("den"))
        return _fmt_qty(n), s[m.end():].strip()
    m = _QTY_MIXED_UF_RE.match(s)
    if m:
        a, b = _UNICODE_FRACTIONS[m.group("uf")]
        return _fmt_qty(int(m.group("int")) + a / b), s[m.end():].strip()
    m = _QTY_UF_RE.match(s)
    if m:
        a, b = _UNICODE_FRACTIONS[m.group("uf")]
        return f"{a}/{b}", s[m.end():].strip()
    m = _QTY_SIMPLE_RE.match(s)
    if m:
        return m.group("n").replace(",", "."), s[m.end():].strip()
    return "", s


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
        qty, unit_txt = _split_leading_qty(rest.strip())
        if qty:
            unite, _ = _match_unit(unit_txt) if unit_txt else ("", "")
            return {"nom": nom.strip(), "quantite": qty, "unite": unite or unit_txt}
        return {"nom": nom.strip(), "quantite": "", "unite": ""}
    # Format source "quantité unité nom" ("200g de pâtes", "1 1/2 l", "½ citron").
    qty, rest = _split_leading_qty(s)
    if not qty or not rest:
        qty, rest = "", s
    unite, nom = _match_unit(rest)
    return {"nom": _clean_ingredient_name(nom), "quantite": qty, "unite": unite}


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
    parts = [_clean_ingredient_name(p) for p in _CONDIMENT_SPLIT_RE.split(nom)]
    parts = [p for p in parts if p]
    if len(parts) >= 2 and all(len(p.split()) <= 4 for p in parts):
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
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
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
    # Replie les ligatures AVANT NFD (œufs/oeufs), retire accents PUIS
    # ponctuation (« 0 % » = « 0% », « d'olive » = « d olive ») pour que ces
    # variantes tombent sur la même clé de regroupement.
    s = s.strip().lower().replace("œ", "oe").replace("æ", "ae")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")  # retire accents
    s = re.sub(r"[^\w\s]", " ", s)  # retire ponctuation (%, (), ', ., ...)
    s = re.sub(r"\s+", " ", s).strip()
    words = [re.sub(r"(?<=\w{3})[sx]$", "", w) for w in s.split(" ") if w]
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


# Unités convertibles : (famille, facteur vers l'unité de base g / ml).
# Clés = forme normalisée (_normalize_form) de l'unité saisie.
_CONVERTIBLE_UNITS: dict[str, tuple[str, float]] = {
    "mg": ("masse", 0.001),
    "g": ("masse", 1.0), "gr": ("masse", 1.0), "grs": ("masse", 1.0),
    "gramme": ("masse", 1.0),
    "kg": ("masse", 1000.0), "kilo": ("masse", 1000.0), "kilogramme": ("masse", 1000.0),
    "ml": ("volume", 1.0), "cl": ("volume", 10.0), "dl": ("volume", 100.0),
    "l": ("volume", 1000.0), "litre": ("volume", 1000.0),
}
# Unités de comptage (arrondies à l'entier supérieur sur la liste de courses).
_COUNT_UNITS = {
    "piece", "gousse", "tranche", "feuille", "sachet", "boite", "pot", "boule",
    "botte", "bouquet", "brin", "barquette", "paquet", "tete",
}
_EPS = 1e-9


def _unit_info(unite: str) -> tuple[str, float] | None:
    """(famille, facteur vers g/ml) si l'unité est une masse ou un volume."""
    return _CONVERTIBLE_UNITS.get(_normalize_form(unite or ""))


def _round_base_amount(n: float) -> float:
    """Grammes / millilitres : arrondi à 5 près (au-dessus) au-delà de 50."""
    if n > 50:
        return math.ceil(n / 5 - _EPS) * 5
    return n


def _fmt_amount(n: float) -> str:
    """Comme _fmt_qty mais jusqu'à 3 décimales (1,255 kg ne perd pas ses 5 g)."""
    n = round(n, 3)
    if n == int(n):
        return str(int(n))
    return f"{n:g}"


def round_for_shopping(item: dict) -> dict:
    """Arrondit la quantité d'un article de la liste de courses (nouveau dict).

    - sans unité ou en unité de comptage (pièce, gousse, sachet…) → entier
      supérieur (« 0.5 poulet » → 1) ;
    - masse / volume → arrondi à 5 g / 5 ml près au-dessus de 50 (calculé dans
      l'unité de base, puis ré-exprimé dans l'unité de l'article) ;
    - autres unités (c. à s., pincée…) et quantités non numériques : inchangé.
    Réservé à la liste de courses : la fiche recette garde les valeurs exactes
    (scale_ingredients ne l'appelle pas).
    """
    new = dict(item)
    n = _parse_qty(item.get("quantite", ""))
    if n is None:
        return new
    unite = (item.get("unite") or "").strip()
    info = _unit_info(unite)
    if info:
        factor = info[1]
        new["quantite"] = _fmt_amount(_round_base_amount(n * factor) / factor)
    elif not unite or _normalize_form(unite) in _COUNT_UNITS:
        new["quantite"] = _fmt_qty(math.ceil(n - _EPS))
    return new


def _display_amount(family: str, base: float, units_seen: set[str]) -> tuple[str, str]:
    """(quantité, unité) d'affichage pour un total exprimé en g ou en ml.

    Masse : kg dès 1 000 g, mg si toutes les entrées étaient en mg (et < 1 g),
    sinon g. Volume : l dès 1 000 ml, cl si une entrée était en cl, sinon ml.
    """
    if family == "masse":
        if base >= 1000:
            return _fmt_amount(base / 1000), "kg"
        if units_seen == {"mg"} and base < 1:
            return _fmt_amount(base * 1000), "mg"
        return _fmt_amount(base), "g"
    if base >= 1000:
        return _fmt_amount(base / 1000), "l"
    if "cl" in units_seen:
        return _fmt_amount(base / 10), "cl"
    return _fmt_amount(base), "ml"


def merge_ingredients(items: list[dict]) -> list[dict]:
    """Fusionne une liste d'ingrédients par (nom, famille d'unité).

    - masses ramenées en grammes, volumes en millilitres, puis additionnés :
      « 1 kg » + « 250 g » de tomates → une ligne « 1.25 kg » ; « 0.5 l » +
      « 25 cl » de lait → « 75 cl » (kg / l dès 1 000 g / ml) ;
    - unités non convertibles (pièce, sachet, c. à s.…) : regroupées par unité
      exacte ; deux unités incompatibles restent sur 2 lignes ;
    - quantités non numériques : on garde la première renseignée ;
    - union des recettes source (champ `recettes`), dans l'ordre de rencontre ;
    - arrondi « courses » en sortie (round_for_shopping).
    Trie par nom.
    """
    out: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    # Clés masse/volume : total en unité de base (None tant qu'aucune quantité
    # numérique) et unités rencontrées (pour choisir l'unité d'affichage).
    bases: dict[tuple[str, str], float | None] = {}
    seen_units: dict[tuple[str, str], set[str]] = {}
    for ing in items:
        nom = (ing.get("nom") or "").strip()
        if not nom:
            continue
        unite = (ing.get("unite") or "").strip()
        qty = str(ing.get("quantite", "") or "").strip()
        srcs = _ingredient_sources(ing)
        n = _parse_qty(qty)
        info = _unit_info(unite)
        # « quelques g » (quantité non numérique) : pas de conversion possible.
        if info is not None and qty and n is None:
            info = None
        # clé normalisée (forme) : regroupe singulier/pluriel + casse/accents,
        # mais conserve distinctes les variantes (oignon rouge / jaune).
        key = (_normalize_form(nom), "~" + info[0] if info else _normalize_form(unite))
        if key not in out:
            out[key] = {
                "nom": nom, "quantite": qty, "unite": unite,
                "recettes": list(dict.fromkeys(srcs)),
            }
            order.append(key)
            if info:
                bases[key] = None
                seen_units[key] = set()
        else:
            existing = out[key]
            if not info:
                a = _parse_qty(existing["quantite"])
                if a is not None and n is not None:
                    existing["quantite"] = _fmt_qty(a + n)
                elif not existing["quantite"] and qty:
                    existing["quantite"] = qty
                # sinon : non sommable → on conserve la première valeur
            for s in srcs:
                if s not in existing["recettes"]:
                    existing["recettes"].append(s)
        if info and n is not None:
            bases[key] = (bases[key] or 0.0) + n * info[1]
            seen_units[key].add(_normalize_form(unite))
    result: list[dict] = []
    for k in order:
        it = out[k]
        if bases.get(k) is not None:
            base = _round_base_amount(bases[k])
            it["quantite"], it["unite"] = _display_amount(k[1][1:], base, seen_units[k])
        result.append(round_for_shopping(it))
    return sorted(result, key=lambda x: x["nom"].lower())
