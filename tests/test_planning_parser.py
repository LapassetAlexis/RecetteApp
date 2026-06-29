"""Tests du parseur de planning LLM (_parse_planning)."""

import pytest

from app.llm_client import LLMClient

# Instancier en mode ollama (défaut) : aucune connexion réseau au __init__.
client = LLMClient()


def test_needs_side_and_is_side():
    # Un « Plat » est complet -> jamais d'accompagnement.
    assert not client._needs_side({"repas": "Plat", "tags": ["Viande"]})
    assert not client._needs_side({"repas": "Plat", "tags": []})
    # Un main NON-Plat (entrée / sans type) reçoit un accompagnement.
    assert client._needs_side({"repas": "", "tags": ["Viande"]})
    assert client._needs_side({"repas": "Entrée", "tags": []})
    # Un accompagnement n'a pas lui-même besoin d'un accompagnement.
    assert not client._needs_side({"repas": "Légume", "tags": []})
    assert client._is_side({"repas": "Légume", "tags": []})
    assert client._is_side({"repas": "Accompagnement", "tags": []})
    assert not client._is_side({"repas": "Plat", "tags": ["Viande"]})


def test_attach_sides_only_non_plat():
    meta = {
        "salade de chèvre": {"nom": "Salade de chèvre", "repas": "", "tags": ["Poisson"]},
        "lasagnes": {"nom": "Lasagnes", "repas": "Plat", "tags": ["Viande"]},
    }
    sides = [{"nom": "Haricots verts", "repas": "Légume", "tags": []}]
    plats = [
        client._make_plat(1, "midi", "Salade de chèvre"),
        client._make_plat(1, "soir", "Lasagnes"),
    ]
    client._attach_sides(plats, meta, sides)
    assert plats[0]["accompagnement"]["nom_recette"] == "Haricots verts"
    assert plats[1]["accompagnement"] is None  # Plat complet


def test_liste_numerotee():
    raw = "\n".join(
        f"{i+1} - Jour {i//2+1} - {'midi' if i%2==0 else 'soir'} - Recette {i+1}"
        for i in range(14)
    )
    plats = client._parse_planning(raw)
    assert len(plats) == 14
    assert plats[0]["jour"] == 1
    assert plats[0]["moment"] == "midi"
    assert plats[1]["moment"] == "soir"


def test_markdown_avec_puces():
    raw = (
        "**Jour 1 - midi**\n- Poulet rôti\n"
        "**Jour 1 - soir**\n- Soupe de légumes\n"
    )
    plats = client._parse_planning(raw)
    noms = [p["nom_recette"] for p in plats]
    assert "Poulet rôti" in noms
    assert "Soupe de légumes" in noms


def test_parenthese_retiree_du_nom():
    plats = client._parse_planning("1 - Jour 1 - midi - Tarte aux pommes (dessert)")
    assert plats[0]["nom_recette"] == "Tarte aux pommes"


def test_limite_a_14():
    raw = "\n".join(
        f"{i+1} - Jour {i//2+1} - {'midi' if i%2==0 else 'soir'} - Recette {i+1}"
        for i in range(20)
    )
    plats = client._parse_planning(raw)
    assert len(plats) == 14


def test_reponse_vide_leve_valueerror():
    with pytest.raises(ValueError):
        client._parse_planning("blabla sans aucun plat reconnaissable")


# ── Nouvelle logique : sélection de noms + assignation des créneaux ──

def test_parse_recipe_names():
    raw = "1 - Tarte\n2. Soupe\n3) Curry (plat principal)\n- Gratin\nTarte"
    assert client._parse_recipe_names(raw) == ["Tarte", "Soupe", "Curry", "Gratin"]


def test_assign_slots_respects_midi_groups_and_no_dup_same_day():
    groups = [1, 1, 2, 2, 2, 3, 4]  # Lun+Mar, Mer+Jeu+Ven, Sam, Dim
    unique = [1, 2, 3, 4]
    names = ["A", "B", "C", "D", "S1", "S2", "S3", "S4", "S5", "S6", "S7"]  # 4 midis + 7 soirs
    plats = client._assign_slots(names, groups, unique)
    midi = {p["jour"]: p["nom_recette"] for p in plats if p["moment"] == "midi"}
    soir = {p["jour"]: p["nom_recette"] for p in plats if p["moment"] == "soir"}

    assert len(plats) == 14
    # midis groupés = même plat
    assert midi[1] == midi[2]            # Lun = Mar
    assert midi[3] == midi[4] == midi[5]  # Mer = Jeu = Ven
    assert midi[6] != midi[3] and midi[7] != midi[6]
    # jamais midi == soir le même jour
    for j in range(1, 8):
        assert midi[j] != soir[j]
    # soirs tous différents
    assert len(set(soir.values())) == 7


def test_assign_slots_empty():
    assert client._assign_slots([], [1, 1, 2, 2, 2, 3, 4], [1, 2, 3, 4]) == []
