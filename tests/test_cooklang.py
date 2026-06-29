"""Tests du parseur Cooklang et du rendu HTML (échappement inclus)."""

from app.cooklang import parse, to_html


def test_parse_metadata_and_ingredients():
    raw = (
        ">> Serves: 6\n"
        ">> Source: https://exemple.fr\n\n"
        "@oignons{2} et @huile d'olive{1%cuillère}\n"
    )
    recipe = parse(raw)
    assert recipe.serves == 6
    assert recipe.source == "https://exemple.fr"
    noms = {i.name for i in recipe.all_ingredients}
    assert "oignons" in noms
    assert "huile d'olive" in noms


def test_parse_serves_invalide_ne_plante_pas():
    recipe = parse(">> Serves: beaucoup\n@sel\n")
    # valeur par défaut conservée, pas de crash
    assert recipe.serves == 4


def test_to_html_echappe_le_html_injecte():
    # Un nom d'ingrédient hostile ne doit pas ressortir en HTML brut
    recipe = parse("@<script>alert(1)</script>{1}\n")
    out = to_html(recipe)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_to_html_garde_les_apostrophes():
    recipe = parse("@huile d'olive{2%cs}\n")
    out = to_html(recipe)
    assert "huile d'olive" in out
    assert "cook-ing" in out
