"""Classement par rayon : mots entiers, mot-clé le plus long, rayon inconnu (L02)."""

import pytest

from app.categories import RAYON_ORDER, categorize, group_by_rayon


@pytest.mark.parametrize("nom, rayon", [
    ("blanc de volaille", "Viande & poisson"),
    ("Blancs de volaille", "Viande & poisson"),
    ("pomme de terre", "Fruits & légumes"),
    ("pommes de terre", "Fruits & légumes"),
    ("pomme", "Fruits & légumes"),
    ("gousse d'ail", "Fruits & légumes"),
    ("poireaux", "Fruits & légumes"),
    ("lait de coco", "Boissons"),
    ("lait", "Crémerie & œufs"),
    ("poivrons", "Fruits & légumes"),
    ("thé vert", "Boissons"),
    ("thym", "Fruits & légumes"),
])
def test_categorize_mots_entiers(nom, rayon):
    assert categorize(nom) == rayon


@pytest.mark.parametrize("nom", ["gâteau de riz", "morceau de sucre", "Gâteaux de riz"])
def test_categorize_pas_de_boissons_par_sous_chaine(nom):
    assert categorize(nom) != "Boissons"


def test_categorize_sous_chaines_ne_matchent_plus():
    # « sel » dans « persil »/« vaisselle », « vin » dans « vinaigre »
    assert categorize("vinaigre balsamique") == "Épicerie salée"
    assert categorize("persil") == "Fruits & légumes"
    assert categorize("xyz inconnu") == "Autre"
    assert categorize("") == "Autre"


def test_group_by_rayon_rayon_inconnu_va_dans_autre():
    items = [
        {"nom": "tomate", "rayon": "Fruits & légumes"},
        {"nom": "truc", "rayon": "Rayon disparu"},
        {"nom": "zzz"},
    ]
    g = group_by_rayon(items)
    autre = next(x for x in g if x["rayon"] == "Autre")
    assert [i["nom"] for i in autre["items"]] == ["truc", "zzz"]
    assert sum(len(x["items"]) for x in g) == 3


def test_group_by_rayon_ordre_magasin_et_rayons_vides_omis():
    g = group_by_rayon([{"nom": "bière"}, {"nom": "carotte"}])
    rayons = [x["rayon"] for x in g]
    assert rayons == ["Fruits & légumes", "Boissons"]
    assert all(r in RAYON_ORDER for r in rayons)
