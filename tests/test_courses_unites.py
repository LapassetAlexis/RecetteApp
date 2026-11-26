"""Moteur de courses : lecture des lignes, conversion d'unités, arrondis (L02)."""

import pytest

from app.text_utils import (
    merge_ingredients,
    parse_ingredient_line,
    round_for_shopping,
    scale_ingredients,
)

# ── Lecture des lignes (B10) ─────────────────────────────────────────

@pytest.mark.parametrize("line, expected", [
    ("1 1/2 l de lait", {"nom": "lait", "quantite": "1.5", "unite": "l"}),
    ("½ citron", {"nom": "citron", "quantite": "1/2", "unite": ""}),
    ("2-3 carottes", {"nom": "carottes", "quantite": "3", "unite": ""}),
    ("2 c. à soupe d'huile", {"nom": "huile", "quantite": "2", "unite": "c. à s."}),
])
def test_parse_lignes_b10(line, expected):
    assert parse_ingredient_line(line) == expected


@pytest.mark.parametrize("line, qty", [
    ("1½ kg de farine", "1.5"),
    ("1 ½ kg de farine", "1.5"),
    ("¼ chou", "1/4"),
    ("¾ l de bouillon", "3/4"),
    ("⅓ botte de radis", "1/3"),
    ("⅔ pot de crème", "2/3"),
    ("2 à 3 gousses d'ail", "3"),
    ("2 – 3 tomates", "3"),
    ("1,5 kg de pommes", "1.5"),
    ("1/2 oignon", "1/2"),
    ("200g de pâtes", "200"),
])
def test_parse_quantites(line, qty):
    assert parse_ingredient_line(line)["quantite"] == qty


@pytest.mark.parametrize("line, unite", [
    ("1 c. à café de sel", "c. à c."),
    ("1 c. à soupe de miel", "c. à s."),
    ("3 cuil. à s. de sucre", "c. à s."),
    ("1 cuil. à c. de cumin", "c. à c."),
    ("1 cuillerée à soupe de miel", "c. à s."),
    ("2 cuillères à café de curry", "c. à c."),
    ("2 c.à.s de sauce soja", "c. à s."),
])
def test_parse_unites_cuilleres(line, unite):
    r = parse_ingredient_line(line)
    assert r["unite"] == unite
    assert r["nom"] and "à" not in r["nom"].split()[0]


def test_parse_legacy_format_avec_nombre_mixte():
    assert parse_ingredient_line("farine : 1 1/2 kg") == {
        "nom": "farine", "quantite": "1.5", "unite": "kg"}


def test_parse_nombre_seul_reste_un_nom():
    assert parse_ingredient_line("100") == {"nom": "100", "quantite": "", "unite": ""}


# ── Conversion et regroupement (B6) ──────────────────────────────────

def test_merge_kg_et_g_une_ligne():
    r = merge_ingredients([
        {"nom": "tomates", "quantite": "1", "unite": "kg"},
        {"nom": "tomate", "quantite": "250", "unite": "g"},
    ])
    assert len(r) == 1
    assert (r[0]["quantite"], r[0]["unite"]) == ("1.25", "kg")
    assert float(r[0]["quantite"]) == 1.25


def test_merge_l_et_cl_une_ligne():
    r = merge_ingredients([
        {"nom": "lait", "quantite": "0.5", "unite": "l"},
        {"nom": "lait", "quantite": "25", "unite": "cl"},
    ])
    assert len(r) == 1
    assert (r[0]["quantite"], r[0]["unite"]) == ("75", "cl")


def test_merge_volume_sans_cl_en_ml_puis_l():
    r = merge_ingredients([
        {"nom": "crème", "quantite": "200", "unite": "ml"},
        {"nom": "crème", "quantite": "0.1", "unite": "l"},
    ])
    assert (r[0]["quantite"], r[0]["unite"]) == ("300", "ml")
    r = merge_ingredients([
        {"nom": "eau", "quantite": "800", "unite": "ml"},
        {"nom": "eau", "quantite": "50", "unite": "cl"},
    ])
    assert (r[0]["quantite"], r[0]["unite"]) == ("1.3", "l")


def test_merge_masse_sous_1kg_en_g():
    r = merge_ingredients([
        {"nom": "farine", "quantite": "0.5", "unite": "kg"},
        {"nom": "farine", "quantite": "200", "unite": "grammes"},
    ])
    assert (r[0]["quantite"], r[0]["unite"]) == ("700", "g")


def test_merge_mg_seuls_restent_en_mg():
    r = merge_ingredients([
        {"nom": "safran", "quantite": "100", "unite": "mg"},
        {"nom": "safran", "quantite": "200", "unite": "mg"},
    ])
    assert (r[0]["quantite"], r[0]["unite"]) == ("300", "mg")


def test_merge_unites_non_convertibles_restent_par_unite():
    r = merge_ingredients([
        {"nom": "huile", "quantite": "2", "unite": "c. à s."},
        {"nom": "huile", "quantite": "1", "unite": "c. à s."},
        {"nom": "huile", "quantite": "10", "unite": "cl"},
    ])
    q = {i["unite"]: i["quantite"] for i in r}
    assert q == {"c. à s.": "3", "cl": "10"}


def test_merge_masse_non_numerique_garde_la_premiere():
    r = merge_ingredients([
        {"nom": "sel", "quantite": "", "unite": "g"},
        {"nom": "sel", "quantite": "3", "unite": "g"},
    ])
    assert len(r) == 1 and (r[0]["quantite"], r[0]["unite"]) == ("3", "g")
    r = merge_ingredients([
        {"nom": "poivre", "quantite": "quelques", "unite": "g"},
        {"nom": "poivre", "quantite": "2", "unite": "g"},
    ])
    assert len(r) == 2  # « quelques g » n'est pas convertible


def test_merge_union_des_recettes_avec_conversion():
    r = merge_ingredients([
        {"nom": "lait", "quantite": "1", "unite": "l", "recette": "Crêpes"},
        {"nom": "lait", "quantite": "20", "unite": "cl", "recette": "Flan"},
    ])
    assert r[0]["recettes"] == ["Crêpes", "Flan"]
    assert (r[0]["quantite"], r[0]["unite"]) == ("1.2", "l")


# ── Arrondis « courses » (B9) ────────────────────────────────────────

def test_round_comptage_entier_superieur():
    assert round_for_shopping({"nom": "poulet", "quantite": "0.5", "unite": ""})["quantite"] == "1"
    assert round_for_shopping({"nom": "ail", "quantite": "1.2", "unite": "gousse"})["quantite"] == "2"
    assert round_for_shopping({"nom": "citron", "quantite": "2", "unite": ""})["quantite"] == "2"


def test_round_grammes_a_5_au_dessus_de_50():
    assert round_for_shopping({"nom": "farine", "quantite": "137", "unite": "g"})["quantite"] == "140"
    assert round_for_shopping({"nom": "farine", "quantite": "135", "unite": "g"})["quantite"] == "135"
    assert round_for_shopping({"nom": "sucre", "quantite": "12.5", "unite": "g"})["quantite"] == "12.5"
    assert round_for_shopping({"nom": "lait", "quantite": "1.234", "unite": "l"})["quantite"] == "1.235"


def test_round_autres_unites_et_non_numerique_inchanges():
    item = {"nom": "huile", "quantite": "1.5", "unite": "c. à s."}
    assert round_for_shopping(item) == item
    item = {"nom": "sel", "quantite": "une pincée", "unite": ""}
    assert round_for_shopping(item) == item


def test_round_ne_modifie_pas_l_entree():
    item = {"nom": "poulet", "quantite": "0.5", "unite": ""}
    round_for_shopping(item)
    assert item["quantite"] == "0.5"


def test_merge_applique_l_arrondi():
    r = merge_ingredients([{"nom": "poulet", "quantite": "0.5", "unite": ""}])
    assert r[0]["quantite"] == "1"
    r = merge_ingredients([{"nom": "farine", "quantite": "137", "unite": "g"}])
    assert (r[0]["quantite"], r[0]["unite"]) == ("140", "g")


def test_merge_arrondi_avant_passage_en_kg():
    r = merge_ingredients([
        {"nom": "riz", "quantite": "998", "unite": "g"},
    ])
    assert (r[0]["quantite"], r[0]["unite"]) == ("1", "kg")


def test_scale_ingredients_garde_les_valeurs_exactes():
    r = scale_ingredients([
        {"nom": "poulet", "quantite": "1", "unite": ""},
        {"nom": "farine", "quantite": "275", "unite": "g"},
    ], 0.5)
    assert r[0]["quantite"] == "0.5"
    assert r[1]["quantite"] == "137.5"
