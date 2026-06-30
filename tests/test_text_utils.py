"""Tests du nettoyage des titres de recettes."""

from app.text_utils import (
    clean_recipe_title, merge_ingredients, normalize_title_case,
    parse_ingredient_line, split_instructions,
)


def test_normalize_title_case_shouting():
    assert normalize_title_case("WRAP D'ÉPINARDS FROMAGE FOUETTÉ ET JAMBON") == \
        "Wrap d'épinards fromage fouetté et jambon"
    assert normalize_title_case("BRUSCHETTAS PARMA") == "Bruschettas parma"


def test_normalize_title_case_leaves_mixed():
    # Casse mixte conservée (préserve noms propres)
    assert normalize_title_case("Steak haché, ratatouille et semoule") == \
        "Steak haché, ratatouille et semoule"
    assert normalize_title_case("Bruschettas Parma") == "Bruschettas Parma"
    assert normalize_title_case("") == ""
    assert normalize_title_case("123") == "123"


def test_clean_recipe_title_normalizes_case():
    assert clean_recipe_title("WRAP D'ÉPINARDS ET JAMBON") == "Wrap d'épinards et jambon"


def test_split_instructions_multiline():
    assert split_instructions("Étape 1\nÉtape 2\nÉtape 3") == ["Étape 1", "Étape 2", "Étape 3"]


def test_split_instructions_single_block_into_sentences():
    txt = "Lavez les épinards. Dans une poêle chauffez l'huile. Enfournez 25 minutes."
    steps = split_instructions(txt)
    assert len(steps) == 3
    assert steps[0] == "Lavez les épinards."
    assert steps[2] == "Enfournez 25 minutes."


def test_split_instructions_empty():
    assert split_instructions("") == []


def test_parse_ingredient_line():
    # On extrait seulement la quantité de tête ; le libellé source reste intact.
    assert parse_ingredient_line("700 g d'épinards") == {"nom": "g d'épinards", "quantite": "700", "unite": ""}
    assert parse_ingredient_line("1/2 cuillère à café de fond de volaille") == {
        "nom": "cuillère à café de fond de volaille", "quantite": "1/2", "unite": ""}
    assert parse_ingredient_line("4 œufs") == {"nom": "œufs", "quantite": "4", "unite": ""}
    # nombre collé à l'unité ("200g")
    assert parse_ingredient_line("200g de pâtes courtes") == {"nom": "g de pâtes courtes", "quantite": "200", "unite": ""}
    assert parse_ingredient_line("80g de roquette") == {"nom": "g de roquette", "quantite": "80", "unite": ""}
    assert parse_ingredient_line("Sel, poivre") == {"nom": "Sel, poivre", "quantite": "", "unite": ""}
    assert parse_ingredient_line("farine : 200 g") == {"nom": "farine", "quantite": "200", "unite": "g"}
    assert parse_ingredient_line("   ") is None


def test_strip_site_suffix_dash():
    assert clean_recipe_title("Flans aux poireaux et chorizo - Amandine Cooking") == "Flans aux poireaux et chorizo"
    assert clean_recipe_title("Wok de boeuf aux légumes - Recettes légères") == "Wok de boeuf aux légumes"


def test_strip_plat_et_recette():
    assert clean_recipe_title("Tiramisu Léger aux Framboises - Plat et Recette") == "Tiramisu Léger aux Framboises"
    assert clean_recipe_title("Wok de Poulet aux légumes et Nouilles - Plat et Recette") == "Wok de Poulet aux légumes et Nouilles"


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


# ── Fusion des ingrédients ──────────────────────────────────────────

def _by_nom(items):
    return {(i["nom"].lower(), i["unite"]): i["quantite"] for i in items}


def test_merge_sums_same_unit():
    r = merge_ingredients([
        {"nom": "Oignon", "quantite": "2", "unite": "pièce"},
        {"nom": "oignon", "quantite": "3", "unite": "pièce"},
    ])
    assert len(r) == 1 and r[0]["quantite"] == "5"


def test_merge_keeps_different_units_separate():
    r = merge_ingredients([
        {"nom": "huile d'olive", "quantite": "2", "unite": "cs"},
        {"nom": "huile d'olive", "quantite": "20", "unite": "cl"},
    ])
    assert len(r) == 2  # ne peut pas additionner cs + cl


def test_merge_handles_decimals_and_fractions():
    r = merge_ingredients([
        {"nom": "farine", "quantite": "1,5", "unite": "kg"},
        {"nom": "farine", "quantite": "1/2", "unite": "kg"},
    ])
    assert r[0]["quantite"] == "2"  # 1.5 + 0.5


def test_merge_non_numeric_keeps_first():
    r = merge_ingredients([
        {"nom": "sel", "quantite": "une pincée", "unite": ""},
        {"nom": "sel", "quantite": "QS", "unite": ""},
    ])
    assert len(r) == 1 and r[0]["quantite"] == "une pincée"


def test_merge_fills_missing_quantity():
    r = merge_ingredients([
        {"nom": "poivre", "quantite": "", "unite": ""},
        {"nom": "poivre", "quantite": "2", "unite": ""},
    ])
    assert r[0]["quantite"] == "2"


def test_merge_singular_plural_and_accents():
    r = merge_ingredients([
        {"nom": "Oignon", "quantite": "1", "unite": "pièce"},
        {"nom": "oignons", "quantite": "2", "unite": "pièces"},   # pluriel + unité pluriel
        {"nom": "Pâtes", "quantite": "100", "unite": "g"},
        {"nom": "pates", "quantite": "150", "unite": "g"},        # sans accent
    ])
    q = {i["nom"].lower(): i["quantite"] for i in r}
    assert len(r) == 2
    assert q["oignon"] == "3"   # oignon + oignons fusionnés
    assert q["pâtes"] == "250"  # accents repliés


def test_merge_keeps_real_variants_separate():
    r = merge_ingredients([
        {"nom": "oignon rouge", "quantite": "1", "unite": ""},
        {"nom": "oignon jaune", "quantite": "1", "unite": ""},
        {"nom": "tomate", "quantite": "2", "unite": ""},
        {"nom": "tomate cerise", "quantite": "10", "unite": ""},
    ])
    noms = {i["nom"] for i in r}
    assert noms == {"oignon rouge", "oignon jaune", "tomate", "tomate cerise"}


def test_merge_skips_empty_names_and_sorts():
    r = merge_ingredients([
        {"nom": "Tomate", "quantite": "2", "unite": ""},
        {"nom": "", "quantite": "1", "unite": ""},
        {"nom": "ail", "quantite": "1", "unite": "gousse"},
    ])
    assert [i["nom"] for i in r] == ["ail", "Tomate"]
