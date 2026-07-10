"""Classement des ingrédients par rayon de magasin (pour la liste de courses).

Heuristique par mots-clés, sans réseau. L'ordre des rayons suit un parcours de
magasin classique (frais d'abord, surgelés en fin)."""

import unicodedata

# Ordre d'affichage = ordre de parcours en magasin.
RAYON_ORDER = [
    "Fruits & légumes",
    "Viande & poisson",
    "Crémerie & œufs",
    "Épicerie salée",
    "Épicerie sucrée",
    "Pain & boulangerie",
    "Surgelés",
    "Boissons",
    "Autre",
]

# Mots-clés par rayon (recherche en sous-chaîne sur le nom normalisé sans accent).
_RAYON_KEYWORDS: dict[str, list[str]] = {
    "Fruits & légumes": [
        "tomate", "oignon", "ail", "carotte", "courgette", "poireau", "pomme de terre",
        "patate", "salade", "laitue", "epinard", "champignon", "poivron", "concombre",
        "aubergine", "brocoli", "chou", "haricot", "petit pois", "citron", "pomme",
        "banane", "fraise", "framboise", "avocat", "echalote", "persil", "basilic",
        "coriandre", "menthe", "gingembre", "celeri", "navet", "potiron", "courge",
        "radis", "betterave", "fenouil", "endive", "orange", "poire", "raisin",
        "abricot", "peche", "ananas", "mangue", "patate douce", "legume",
        "roquette", "melon", "pasteque", "clementine", "mandarine", "kiwi",
        "cerise", "myrtille", "cassis", "rhubarbe", "artichaut", "asperge",
        "epinard", "mache", "cresson", "ciboulette", "estragon", "aneth", "thym",
        "romarin", "laurier", "cebette", "shiitake",
    ],
    "Viande & poisson": [
        "boeuf", "porc", "poulet", "dinde", "veau", "agneau", "lardon", "jambon",
        "saucisse", "steak", "viande", "saumon", "thon", "cabillaud", "crevette",
        "poisson", "merlu", "colin", "truite", "moule", "calamar", "chorizo", "bacon",
        "escalope", "filet", "cuisse", "magret", "merguez",
    ],
    "Crémerie & œufs": [
        "oeuf", "lait", "creme", "beurre", "fromage", "yaourt", "parmesan", "mozzarella",
        "chevre", "feta", "emmental", "gruyere", "ricotta", "mascarpone", "comte",
        "cheddar", "boursin", "fromage blanc", "petit suisse", "margarine",
        "toastinette", "kiri", "raclette", "burrata", "skyr", "creme fraiche",
    ],
    "Épicerie salée": [
        "pates", "riz", "farine", "huile", "vinaigre", "sel", "poivre", "epice",
        "conserve", "tomate concassee", "concentre", "bouillon", "moutarde", "ketchup",
        "mayonnaise", "lentille", "pois chiche", "haricot sec", "semoule", "quinoa",
        "boulgour", "couscous", "sauce soja", "curry", "cumin", "paprika", "olive",
        "cornichon", "cube", "levure", "maizena", "polenta", "pesto", "tapenade",
        "sauce tomate", "passata", "chapelure", "gnocchi", "nouille",
    ],
    "Épicerie sucrée": [
        "sucre", "chocolat", "vanille", "miel", "confiture", "caramel", "compote",
        "biscuit", "cacao", "amande", "noisette", "noix", "sirop", "pepite", "nutella",
        "sucre vanille", "fruits secs", "raisin sec",
    ],
    "Pain & boulangerie": [
        "pain", "baguette", "brioche", "pate brisee", "pate feuilletee", "pate a tarte",
        "pate sablee", "tortilla", "wrap", "pita", "crouton", "biscotte",
    ],
    "Surgelés": ["surgele", "glace", "epinard surgele", "poisson pane"],
    "Boissons": [
        "eau", "jus", "vin", "biere", "soda", "cafe", "the", "lait de coco",
        "boisson", "cidre",
    ],
}


def _norm(s: str) -> str:
    # Replie les ligatures AVANT NFD (qui ne décompose pas œ/æ) → « œuf » = « oeuf ».
    s = s.lower().strip().replace("œ", "oe").replace("æ", "ae")
    s = "".join(c for c in unicodedata.normalize("NFD", s)
                if unicodedata.category(c) != "Mn")
    return s


def categorize(nom: str) -> str:
    """Retourne le rayon d'un ingrédient (« Autre » si rien ne matche)."""
    n = _norm(nom)
    best = ("Autre", 0)
    for rayon, mots in _RAYON_KEYWORDS.items():
        for m in mots:
            # match plus long = plus spécifique (ex. « pomme de terre » > « pomme »)
            if m in n and len(m) > best[1]:
                best = (rayon, len(m))
    return best[0]


def group_by_rayon(items: list[dict]) -> list[dict]:
    """Groupe une liste d'ingrédients par rayon, dans l'ordre du magasin.
    Retourne [{rayon, items:[...]}] (rayons vides omis)."""
    buckets: dict[str, list[dict]] = {r: [] for r in RAYON_ORDER}
    for it in items:
        buckets.setdefault(it.get("rayon") or categorize(it.get("nom", "")), []).append(it)
    return [{"rayon": r, "items": buckets[r]} for r in RAYON_ORDER if buckets.get(r)]
