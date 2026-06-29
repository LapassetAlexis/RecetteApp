"""Tests de l'estimation nutritionnelle."""

from app.nutrition import estimate_nutrition, _to_grams, _match_food


def test_to_grams_units_and_pieces():
    assert _to_grams("200", "g de pâtes") == 200
    assert _to_grams("1", "kg de farine") == 1000
    assert _to_grams("20", "cl de crème") == 200
    assert _to_grams("2", "courgettes") == 400      # 2 × 200 g
    assert _to_grams("1", "oignon") == 110
    assert _to_grams("", "huile d'olive") is None   # pas de quantité


def test_to_grams_uses_unite_field():
    # ancien modèle : unité dans le champ unite (pas dans le libellé)
    assert _to_grams("700", "d'épinards", "g") == 700
    assert _to_grams("150", "lait demi écrémé", "ml") == 150
    assert _to_grams("1/2", "fond de volaille", "cuillère") == 7.5


def test_to_grams_clamps_absurd():
    # nombre sans unité ni pièce connue -> défaut ×100 dépasse le plafond -> None
    assert _to_grams("700", "d'épinards", "") is None


def test_match_food():
    assert _match_food("g de pâtes courtes")[0] == 350      # kcal/100g pâtes
    assert _match_food("boule de mozzarella")[0] == 280
    assert _match_food("truc inconnu xyz") is None


def test_estimate_nutrition_per_portion():
    ings = [
        {"nom": "g de pâtes", "quantite": "200", "unite": ""},   # 200 g × 350 = 700 kcal
        {"nom": "boule de mozzarella", "quantite": "1", "unite": ""},  # 125 g × 280 = 350 kcal
    ]
    n = estimate_nutrition(ings, servings=2)
    # total kcal ~1050 / 2 parts ~525
    assert 480 <= n["calories"] <= 560
    assert n["matched"] == 2 and n["total"] == 2
    assert n["source"] == "estimation"


def test_estimate_nutrition_no_match():
    assert estimate_nutrition([{"nom": "machin inconnu", "quantite": "1", "unite": ""}], 4) is None
    assert estimate_nutrition([], 4) is None
