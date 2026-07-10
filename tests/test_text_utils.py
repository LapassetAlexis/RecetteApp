"""Tests du nettoyage des titres de recettes."""

from app.text_utils import (
    clean_recipe_title, merge_ingredients, normalize_cached_ingredient,
    normalize_title_case, parse_ingredient_line, split_instructions,
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
    # Normalisation en amont : quantité + unité canonique extraites, nom propre.
    assert parse_ingredient_line("700 g d'épinards") == {"nom": "épinards", "quantite": "700", "unite": "g"}
    assert parse_ingredient_line("1/2 cuillère à café de fond de volaille") == {
        "nom": "fond de volaille", "quantite": "1/2", "unite": "c. à c."}
    assert parse_ingredient_line("4 œufs") == {"nom": "œufs", "quantite": "4", "unite": ""}
    # nombre collé à l'unité ("200g")
    assert parse_ingredient_line("200g de pâtes courtes") == {"nom": "pâtes courtes", "quantite": "200", "unite": "g"}
    assert parse_ingredient_line("80g de roquette") == {"nom": "roquette", "quantite": "80", "unite": "g"}
    # abréviations d'unité normalisées, même sans quantité
    assert parse_ingredient_line("Cs huile") == {"nom": "huile", "quantite": "", "unite": "c. à s."}
    assert parse_ingredient_line("2 CaS de crème") == {"nom": "crème", "quantite": "2", "unite": "c. à s."}
    assert parse_ingredient_line("1 gousse d'ail") == {"nom": "ail", "quantite": "1", "unite": "gousse"}
    # pas d'unité → nom intact (ne pas manger un mot qui commence comme une unité)
    assert parse_ingredient_line("cassonade") == {"nom": "cassonade", "quantite": "", "unite": ""}
    assert parse_ingredient_line("lardons") == {"nom": "lardons", "quantite": "", "unite": ""}
    assert parse_ingredient_line("Sel, poivre") == {"nom": "Sel, poivre", "quantite": "", "unite": ""}
    assert parse_ingredient_line("farine : 200 g") == {"nom": "farine", "quantite": "200", "unite": "g"}
    assert parse_ingredient_line("   ") is None


def test_normalize_cached_ingredient_fixes_leaked_unit():
    # Ancien cache « sale » : l'unité avait fui dans le nom.
    assert normalize_cached_ingredient({"nom": "G de farine complète", "quantite": "250", "unite": ""}) == \
        [{"nom": "farine complète", "quantite": "250", "unite": "g"}]
    assert normalize_cached_ingredient({"nom": "Cs huile", "quantite": "0.75", "unite": ""}) == \
        [{"nom": "huile", "quantite": "0.75", "unite": "c. à s."}]
    # déjà propre → idempotent
    assert normalize_cached_ingredient({"nom": "épinards", "quantite": "700", "unite": "g"}) == \
        [{"nom": "épinards", "quantite": "700", "unite": "g"}]


def test_normalize_cached_ingredient_splits_condiments():
    assert normalize_cached_ingredient({"nom": "Sel, poivre.", "quantite": "", "unite": ""}) == \
        [{"nom": "Sel", "quantite": "", "unite": ""}, {"nom": "poivre", "quantite": "", "unite": ""}]
    assert normalize_cached_ingredient({"nom": "Sel et poivre", "quantite": "", "unite": ""}) == \
        [{"nom": "Sel", "quantite": "", "unite": ""}, {"nom": "poivre", "quantite": "", "unite": ""}]
    # ne casse pas un nom composé long
    assert normalize_cached_ingredient({"nom": "Poivre du moulin", "quantite": "", "unite": ""}) == \
        [{"nom": "Poivre du moulin", "quantite": "", "unite": ""}]


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


def test_clean_ingredient_name():
    # parenthèses/commentaires retirés
    assert parse_ingredient_line("Mozzarella (ou feta)")["nom"] == "Mozzarella"
    assert parse_ingredient_line("Ketchup ...)")["nom"] == "Ketchup"
    # article de tête retiré
    assert parse_ingredient_line("Un poireau")["nom"] == "poireau"
    # mot de préparation retiré (regroupement)
    assert parse_ingredient_line("Ail haché")["nom"] == "Ail"
    assert parse_ingredient_line("Salade essorée")["nom"] == "Salade"
    assert parse_ingredient_line("Pâtes cuites al dente")["nom"] == "Pâtes"
    # qualificatifs porteurs de sens conservés
    assert parse_ingredient_line("crème fraîche")["nom"] == "crème fraîche"
    assert parse_ingredient_line("oignon rouge")["nom"] == "oignon rouge"


def test_normalize_form_merges_ligature_and_punct():
    # œ/oe, espace avant %, préparation → même clé de fusion
    oeufs = normalize_cached_ingredient({"nom": "Œufs", "quantite": "4", "unite": ""})
    oeufs += normalize_cached_ingredient({"nom": "Oeufs", "quantite": "3", "unite": ""})
    r = merge_ingredients(oeufs)
    assert len(r) == 1 and r[0]["quantite"] == "7"

    fb = normalize_cached_ingredient({"nom": "Fromage blanc 0 %", "quantite": "150", "unite": "g"})
    fb += normalize_cached_ingredient({"nom": "Fromage blanc 0%", "quantite": "100", "unite": "g"})
    r = merge_ingredients(fb)
    assert len(r) == 1 and r[0]["quantite"] == "250"

    ail = normalize_cached_ingredient({"nom": "Ail", "quantite": "2", "unite": "gousse"})
    ail += normalize_cached_ingredient({"nom": "Ail hachées", "quantite": "1.5", "unite": "gousse"})
    r = merge_ingredients(ail)
    assert len(r) == 1 and r[0]["quantite"] == "3.5"


def test_split_condiments_compound_line():
    parts = normalize_cached_ingredient(
        {"nom": "Le jus d'un citron, huile d'olive, sel, poivre", "quantite": "", "unite": ""})
    noms = {p["nom"] for p in parts}
    assert "huile d'olive" in noms and "sel" in noms and "poivre" in noms


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


# ── Catégorisation par rayon ─────────────────────────────────────────

def test_categorize_ligature_and_new_items():
    from app.categories import categorize
    assert categorize("Œufs") == "Crémerie & œufs"      # ligature œ repliée
    assert categorize("Pesto verde") == "Épicerie salée"
    assert categorize("Melon bien mûr") == "Fruits & légumes"
    assert categorize("Roquette") == "Fruits & légumes"
    assert categorize("Toastinette") == "Crémerie & œufs"
    assert categorize("crème fraîche") == "Crémerie & œufs"
