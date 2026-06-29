"""Estimation nutritionnelle (façon CIQUAL) à partir d'ingrédients.

Approche : table d'aliments génériques (kcal/protéines/glucides/lipides pour
100 g), table de poids moyens par pièce, conversions d'unités → on convertit
chaque ingrédient en grammes, on matche par mots-clés, on somme, puis on divise
par le nombre de parts.

Volontairement approximatif (~10-20 %) : le matching par nom et la conversion
quantité→grammes ne sont jamais parfaits. À afficher comme une ESTIMATION.
"""

import re
import unicodedata

# kcal, protéines (g), glucides (g), lipides (g) pour 100 g
# Valeurs génériques (ordre de grandeur CIQUAL/tables courantes).
FOOD_DB: dict[str, tuple[float, float, float, float]] = {
    # Féculents / céréales
    "pates": (350, 12, 70, 1.5), "pate": (350, 12, 70, 1.5), "spaghetti": (350, 12, 70, 1.5),
    "riz": (350, 7, 78, 1), "farine": (350, 10, 73, 1), "semoule": (350, 12, 73, 1),
    "pain": (260, 9, 50, 3), "quinoa": (360, 14, 64, 6), "lentille": (330, 24, 50, 1.5),
    "pomme de terre": (80, 2, 17, 0.1), "patate": (80, 2, 17, 0.1),
    # Légumes
    "courgette": (17, 1.2, 3, 0.3), "tomate": (18, 0.9, 3.5, 0.2), "oignon": (40, 1.1, 9, 0.1),
    "ail": (130, 6, 28, 0.5), "carotte": (35, 0.8, 7, 0.2), "epinard": (23, 2.9, 1, 0.4),
    "salade": (15, 1.2, 2, 0.2), "roquette": (25, 2.6, 2, 0.7), "poivron": (26, 1, 5, 0.3),
    "champignon": (22, 3, 1, 0.3), "courge": (26, 1, 5, 0.1), "celeri": (16, 0.7, 3, 0.2),
    "haricot": (31, 1.8, 5, 0.2), "petit pois": (80, 5, 14, 0.4), "brocoli": (34, 2.8, 4, 0.4),
    "aubergine": (25, 1, 6, 0.2), "concombre": (15, 0.7, 3.6, 0.1), "radis": (16, 0.7, 3.4, 0.1),
    # Protéines animales
    "poulet": (165, 31, 0, 3.6), "boeuf": (250, 26, 0, 15), "boeuf hache": (250, 26, 0, 17),
    "porc": (242, 27, 0, 14), "jambon": (145, 18, 1, 7), "lardon": (300, 15, 0, 27),
    "saumon": (208, 20, 0, 13), "thon": (130, 28, 0, 1), "cabillaud": (82, 18, 0, 0.7),
    "crevette": (99, 24, 0, 0.3), "oeuf": (155, 13, 1, 11), "dinde": (135, 29, 0, 1.7),
    "saucisse": (300, 13, 2, 27),
    # Produits laitiers / fromages
    "lait": (50, 3.3, 5, 1.6), "creme": (290, 2.5, 3, 30), "creme fraiche": (290, 2.5, 3, 30),
    "beurre": (740, 0.8, 0.5, 82), "fromage": (350, 25, 1, 28), "parmesan": (430, 38, 0, 29),
    "mozzarella": (280, 18, 2, 22), "gruyere": (380, 27, 0, 30), "chevre": (290, 19, 2, 23),
    "yaourt": (60, 4, 5, 3), "ricotta": (170, 11, 3, 13), "feta": (260, 14, 4, 21),
    # Matières grasses / divers
    "huile": (900, 0, 0, 100), "huile d'olive": (900, 0, 0, 100), "pesto": (450, 5, 6, 45),
    "sucre": (400, 0, 100, 0), "miel": (320, 0.3, 80, 0), "chocolat": (550, 6, 50, 35),
    "creme de coco": (200, 2, 3, 20), "lait de coco": (200, 2, 3, 20),
    "tomate concassee": (30, 1.5, 5, 0.2), "puree de tomate": (35, 1.7, 6, 0.3),
    "vin": (85, 0.1, 2.6, 0), "bouillon": (5, 0.5, 0.5, 0.1), "fond de volaille": (5, 0.5, 0.5, 0.1),
}

# Poids moyen en grammes pour les ingrédients comptés en pièces
PIECE_WEIGHTS: dict[str, float] = {
    "oeuf": 50, "oignon": 110, "courgette": 200, "tomate": 120, "carotte": 80,
    "gousse": 5, "ail": 5, "pomme de terre": 150, "poivron": 150, "echalote": 30,
    "boule de mozzarella": 125, "mozzarella": 125, "citron": 100, "pomme": 150,
    "banane": 120, "aubergine": 250, "concombre": 300, "branche": 40, "tranche": 20,
}

# Conversion d'une unité vers des grammes (approximatif, base eau pour les liquides)
UNIT_GRAMS: dict[str, float] = {
    "g": 1, "gr": 1, "kg": 1000, "mg": 0.001,
    "ml": 1, "cl": 10, "dl": 100, "l": 1000,
    "cs": 15, "càs": 15, "cuillere a soupe": 15, "cuillere": 15,
    "cc": 5, "càc": 5, "cuillere a cafe": 5,
    "pincee": 1, "gousse": 5, "tranche": 20, "sachet": 8, "verre": 200,
    "tasse": 240, "botte": 100, "boite": 400, "brin": 2, "feuille": 2,
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip()


def _match_food(label: str) -> tuple[float, float, float, float] | None:
    """Trouve l'aliment de la table dont un mot-clé apparaît dans le libellé.
    On privilégie la clé la plus longue (plus spécifique)."""
    n = _norm(label)
    best = None
    best_len = 0
    for key, macros in FOOD_DB.items():
        if key in n and len(key) > best_len:
            best, best_len = macros, len(key)
    return best


_MAX_GRAMS = 5000  # garde-fou : au-delà, c'est une erreur de parsing → on ignore


def _to_grams(quantite: str, label: str, unite: str = "") -> float | None:
    """Convertit (quantité, libellé, unité) en grammes. None si indéterminable."""
    n = _norm(label)
    qty = None
    s = str(quantite).strip().replace(",", ".")
    if s:
        fr = re.fullmatch(r"(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)", s)
        if fr and float(fr.group(2)):
            qty = float(fr.group(1)) / float(fr.group(2))
        elif re.fullmatch(r"\d+(?:\.\d+)?", s):
            qty = float(s)

    grams = None
    # 0) unité explicite dans le champ unite (ancien modèle structuré)
    u = _norm(unite)
    if u and u in UNIT_GRAMS:
        grams = (qty if qty is not None else 1) * UNIT_GRAMS[u]
    # 1) cuillères composées (café ≈ 5 g, soupe ≈ 15 g), gère le pluriel
    if grams is None:
        sp = re.search(r"\bcuill?[eè]res?\s+[aà]\s+(caf[eé]|soupe)", n)
        if not sp:
            sp = re.match(r"^c\.?\s*[aà]\.?\s*(c|s)\b", n)  # "c. à s.", "c à c"
        if sp:
            g = sp.group(1)
            grams = (qty if qty is not None else 1) * (5 if g in ("cafe", "café", "c") else 15)
    # 2) sinon unité en tête du libellé (ex. "g de pâtes"), pluriel toléré
    if grams is None:
        for unit, g in sorted(UNIT_GRAMS.items(), key=lambda kv: -len(kv[0])):
            if re.match(rf"^{re.escape(unit)}s?\b", n):
                grams = (qty if qty is not None else 1) * g
                break
    # 2) compté en pièces (ex. "2 courgettes", "1 oignon")
    if grams is None and qty is not None:
        for key, w in sorted(PIECE_WEIGHTS.items(), key=lambda kv: -len(kv[0])):
            if key in n:
                grams = qty * w
                break
        if grams is None:
            grams = qty * 100  # défaut prudent : 1 "unité" ~ 100 g
    if grams is None or grams > _MAX_GRAMS:
        return None
    return grams


def estimate_nutrition(ingredients: list[dict], servings: int = 4) -> dict | None:
    """Estime les valeurs nutritionnelles PAR PART à partir des ingrédients.

    Renvoie {calories, proteines, glucides, lipides, matched, total, source}
    ou None si rien d'exploitable. `matched/total` = qualité du calcul.
    """
    if not ingredients or servings <= 0:
        return None
    tot = {"calories": 0.0, "proteines": 0.0, "glucides": 0.0, "lipides": 0.0}
    matched = 0
    for ing in ingredients:
        label = ing.get("nom", "")
        grams = _to_grams(ing.get("quantite", ""), label, ing.get("unite", ""))
        macros = _match_food(label)
        if grams is None or macros is None:
            continue
        matched += 1
        f = grams / 100.0
        tot["calories"] += macros[0] * f
        tot["proteines"] += macros[1] * f
        tot["glucides"] += macros[2] * f
        tot["lipides"] += macros[3] * f
    if matched == 0:
        return None
    ratio = matched / len(ingredients)
    if ratio >= 0.7:
        confiance = "Bonne"
    elif ratio >= 0.4:
        confiance = "Moyenne"
    else:
        confiance = "Mauvaise"
    return {
        "calories": round(tot["calories"] / servings),
        "proteines": round(tot["proteines"] / servings, 1),
        "glucides": round(tot["glucides"] / servings, 1),
        "lipides": round(tot["lipides"] / servings, 1),
        "matched": matched,
        "total": len(ingredients),
        "source": "estimation",
        "confiance": confiance,
    }
