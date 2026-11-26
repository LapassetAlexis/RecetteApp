"""Moteur d'ingrédients v2 (M03, N13) : unités de comptage, condiments,
parenthèses gardées, rayons des expressions composées."""

import pytest

from app.categories import categorize
from app.text_utils import (
    merge_ingredients,
    normalize_cached_ingredient,
    parse_ingredient_line,
    parse_ingredient_lines,
)

# ── Unités de comptage ───────────────────────────────────────────────


def test_branches_de_thym():
    assert parse_ingredient_line("2 branches de thym") == {
        "nom": "thym", "quantite": "2", "unite": "branche"}


@pytest.mark.parametrize("line, unite, nom", [
    ("1 branche de romarin", "branche", "romarin"),
    ("3 brins de persil", "brin", "persil"),
    ("1 brin d'aneth", "brin", "aneth"),
    ("4 feuilles de laurier", "feuille", "laurier"),
    ("2 tiges de citronnelle", "tige", "citronnelle"),
    ("1 tige de céleri", "tige", "céleri"),
    ("4 tranches de jambon", "tranche", "jambon"),
    ("1 botte de radis", "botte", "radis"),
    ("2 sachets de levure", "sachet", "levure"),
    ("1 boîte de thon", "boîte", "thon"),
    ("2 boites de pois chiches", "boîte", "pois chiches"),
    ("1 pot de crème", "pot", "crème"),
    ("1 bouquet de coriandre", "bouquet", "coriandre"),
    ("2 bouquets garnis", "bouquet", "garnis"),
])
def test_unites_de_comptage(line, unite, nom):
    r = parse_ingredient_line(line)
    assert r["unite"] == unite
    assert r["nom"] == nom


def test_unite_de_comptage_ne_mange_pas_un_mot():
    # « tigelle », « potiron », « branchies » ne sont pas des unités.
    assert parse_ingredient_line("1 potiron")["nom"] == "potiron"
    assert parse_ingredient_line("2 tigelles")["unite"] == ""


def test_normalize_ne_recolle_pas_une_unite_connue():
    assert normalize_cached_ingredient({"nom": "thym", "quantite": "2", "unite": "branche"}) == [
        {"nom": "thym", "quantite": "2", "unite": "branche"}]
    assert normalize_cached_ingredient({"nom": "thym", "quantite": "2", "unite": "branches"}) == [
        {"nom": "thym", "quantite": "2", "unite": "branche"}]


def test_normalize_garde_une_unite_inconnue_a_part():
    assert normalize_cached_ingredient({"nom": "cannelle", "quantite": "1", "unite": "bâton"}) == [
        {"nom": "cannelle", "quantite": "1", "unite": "bâton"}]


def test_normalize_repare_un_ancien_cache_branches_thym():
    # Ancien cache : l'unité inconnue avait fui dans le nom.
    assert normalize_cached_ingredient({"nom": "Branches thym", "quantite": "2", "unite": ""}) == [
        {"nom": "thym", "quantite": "2", "unite": "branche"}]


def test_normalize_sans_quantite_garde_le_nombre_du_nom():
    assert normalize_cached_ingredient({"nom": "4 épices", "quantite": "", "unite": "pincée"}) == [
        {"nom": "4 épices", "quantite": "", "unite": "pincée"}]


def test_normalize_corrige_une_unite_restee_dans_le_nom():
    assert normalize_cached_ingredient({"nom": "g de farine", "quantite": "200", "unite": "g"}) == [
        {"nom": "farine", "quantite": "200", "unite": "g"}]


def test_branches_fusionnent():
    items = normalize_cached_ingredient({"nom": "thym", "quantite": "2", "unite": "branche"})
    items += normalize_cached_ingredient({"nom": "Branches thym", "quantite": "1", "unite": ""})
    r = merge_ingredients(items)
    assert len(r) == 1 and r[0]["quantite"] == "3" and r[0]["unite"] == "branche"


# ── Condiments ───────────────────────────────────────────────────────


@pytest.mark.parametrize("line", ["sel, poivre", "Sel et poivre", "sel & poivre", "Sel, poivre."])
def test_sel_poivre_deux_lignes(line):
    r = parse_ingredient_lines(line)
    assert [i["nom"].lower() for i in r] == ["sel", "poivre"]
    assert all(i["quantite"] == "" and i["unite"] == "" for i in r)


def test_condiments_composes():
    r = parse_ingredient_lines("Fleur de sel et poivre du moulin")
    assert [i["nom"] for i in r] == ["Fleur de sel", "poivre du moulin"]


@pytest.mark.parametrize("line", [
    "farine, tamisée",
    "huile, sel, poivre",
    "tomates, oignons",
    "poulet à la crème et estragon",
])
def test_autres_lignes_a_virgule_non_decoupees(line):
    assert len(parse_ingredient_lines(line)) == 1


def test_ligne_quantifiee_non_decoupee():
    assert parse_ingredient_lines("1 pincée de sel, poivre") == [
        parse_ingredient_line("1 pincée de sel, poivre")]


def test_parse_lines_vide():
    assert parse_ingredient_lines("   ") == []


def test_parse_ingredient_line_garde_la_ligne_entiere():
    # Compatibilité : la fonction historique renvoie toujours un seul dict.
    assert parse_ingredient_line("Sel, poivre")["nom"] == "Sel, poivre"


# ── Parenthèses ──────────────────────────────────────────────────────


def test_keep_parens():
    assert parse_ingredient_line("Papier toilette (x6)", keep_parens=True) == {
        "nom": "Papier toilette (x6)", "quantite": "", "unite": ""}
    assert parse_ingredient_line("2 l lait (bio)", keep_parens=True) == {
        "nom": "lait (bio)", "quantite": "2", "unite": "l"}
    assert parse_ingredient_lines("Papier toilette (x6)", keep_parens=True)[0]["nom"] == \
        "Papier toilette (x6)"


def test_parens_retirees_par_defaut():
    assert parse_ingredient_line("Papier toilette (x6)")["nom"] == "Papier toilette"


# ── Rayons ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("nom", [
    "lait de coco", "Lait de coco", "crème de coco", "concentré de tomate",
    "concentré de tomates", "bouillon", "bouillon de volaille", "bouillon de légumes",
    "bouillon de bœuf", "fond de veau",
])
def test_rayon_epicerie_salee(nom):
    assert categorize(nom) == "Épicerie salée"


@pytest.mark.parametrize("nom, rayon", [
    ("lait", "Crémerie & œufs"),
    ("crème fraîche", "Crémerie & œufs"),
    ("blanc de volaille", "Viande & poisson"),
    ("thé vert", "Boissons"),
])
def test_rayons_inchanges(nom, rayon):
    assert categorize(nom) == rayon
