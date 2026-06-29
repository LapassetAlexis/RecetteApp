"""Petites fonctions de nettoyage de texte partagées (app + extraction)."""

import re

# Marqueurs typiques d'un suffixe « source » à retirer du titre d'une recette.
_SOURCE_MARKERS = (
    "cooking", "recettes", "recette ", "cuisine", "marmiton", "750g",
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
