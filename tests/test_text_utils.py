"""Tests du nettoyage des titres de recettes."""

from app.text_utils import clean_recipe_title


def test_strip_site_suffix_dash():
    assert clean_recipe_title("Flans aux poireaux et chorizo - Amandine Cooking") == "Flans aux poireaux et chorizo"
    assert clean_recipe_title("Wok de boeuf aux légumes - Recettes légères") == "Wok de boeuf aux légumes"


def test_strip_pipe_suffix():
    assert clean_recipe_title("Wrap de poulet à la grecque | Recette Minceur") == "Wrap de poulet à la grecque"


def test_strip_ww():
    assert clean_recipe_title("Pâtes au Thon et aux Légumes WW") == "Pâtes au Thon et aux Légumes"


def test_keeps_legit_dash():
    # le segment de droite n'est pas une source -> on garde tel quel
    assert clean_recipe_title("Boeuf - carottes") == "Boeuf - carottes"


def test_plain_titles_unchanged():
    assert clean_recipe_title("Quiche sans pâte au thon") == "Quiche sans pâte au thon"
    assert clean_recipe_title("galette de pomme de terre au jambon") == "galette de pomme de terre au jambon"
    assert clean_recipe_title("") == ""
