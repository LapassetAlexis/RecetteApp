"""Tests du parseur de planning LLM (_parse_planning)."""

import pytest

from app.llm_client import LLMClient

# Instancier en mode ollama (défaut) : aucune connexion réseau au __init__.
client = LLMClient()


def test_needs_side_and_is_side():
    # Plat complet (type « Plat » ou tag « plat ») : se suffit, pas d'accompagnement.
    assert not client._needs_side({"repas": "Plat", "base": ["Viande"]})
    assert not client._needs_side({"repas": "", "tags": ["Plat"]})
    # Plat principal non complet : accompagnement par défaut.
    assert client._needs_side({"repas": "", "tags": []})
    assert client._needs_side({"repas": "Entrée", "tags": []})
    # Un accompagnement (Base = Légume) n'en reçoit pas lui-même.
    assert not client._needs_side({"repas": "", "base": ["Légume"]})
    # _is_side : Base contient « Légume » ET Nature = Recette.
    assert client._is_side({"base": ["Légume"]})
    assert client._is_side({"repas": "Entrée", "base": ["Légume"]})
    assert not client._is_side({"base": ["Légume"], "nature": "Ingrédient"})  # brique
    assert not client._is_side({"repas": "Plat", "base": ["Viande"]})
    assert client._is_complete({"repas": "Plat", "tags": []})
    assert client._is_complete({"repas": "", "tags": ["plat"]})


def test_attach_sides_skips_complete_plat():
    meta = {
        "lasagnes": {"nom": "Lasagnes", "repas": "Plat", "base": ["Viande"]},
        "poulet": {"nom": "Poulet", "repas": "", "base": ["Viande"]},  # non complet -> côté
        "riz pilaf": {"nom": "Riz pilaf", "repas": "", "base": ["Légume"]},  # side
    }
    sides = [{"nom": "Haricots verts", "repas": "", "base": ["Légume"]}]
    plats = [
        client._make_plat(1, "midi", "Lasagnes"),
        client._make_plat(1, "soir", "Poulet"),
        client._make_plat(2, "soir", "Riz pilaf"),
    ]
    client._attach_sides(plats, meta, sides)
    assert plats[0]["accompagnement"] is None  # plat complet -> aucun
    assert plats[1]["accompagnement"]["nom_recette"] == "Haricots verts"  # nature -> côté
    assert plats[2]["accompagnement"] is None  # accompagnement (Base=Légume) -> aucun


def test_attach_sides_same_plat_same_side():
    """Un même plat sur jours consécutifs garde le même accompagnement
    (préserve la fusion des cases du planning)."""
    meta = {
        "poulet": {"nom": "Poulet", "repas": "", "base": ["Viande"]},
        "poisson": {"nom": "Poisson", "repas": "", "base": ["Poisson"]},
    }
    sides = [
        {"nom": "Haricots", "repas": "", "base": ["Légume"]},
        {"nom": "Riz", "repas": "", "base": ["Légume"]},
    ]
    plats = [
        client._make_plat(1, "midi", "Poulet"),
        client._make_plat(2, "midi", "Poulet"),
        client._make_plat(3, "midi", "Poisson"),
    ]
    client._attach_sides(plats, meta, sides)
    assert plats[0]["accompagnement"]["nom_recette"] == plats[1]["accompagnement"]["nom_recette"]
    assert plats[2]["accompagnement"]["nom_recette"] != plats[0]["accompagnement"]["nom_recette"]


def test_season_rank_and_recipe_seasons():
    ete = client._norm("Été")
    assert client._season_rank({"tags": ["Été", "Salade"]}, ete) == 0   # saison demandée
    assert client._season_rank({"tags": ["Salade"]}, ete) == 1          # toutes saisons
    assert client._season_rank({"tags": ["Hiver"]}, ete) == 2           # autre saison
    assert client._recipe_seasons({"tags": ["Hiver", "Soupe"]}) == {"hiver"}
    assert client._recipe_seasons({"tags": ["Soupe"]}) == set()


def test_weather_and_meteo_rank():
    assert client._weather("Froid (< 10°C)") == "froid"
    assert client._weather("Frais (10-18°C)") == "froid"
    assert client._weather("Chaud (> 25°C)") == "chaud"
    assert client._weather("Canicule (> 35°C)") == "chaud"
    assert client._weather("Doux (18-25°C)") == ""
    # froid -> plat chaud privilégié, plat froid évité
    assert client._meteo_rank({"tags": ["Plat chaud"]}, "froid") == 0
    assert client._meteo_rank({"tags": ["Plat froid"]}, "froid") == 2
    assert client._meteo_rank({"tags": ["Soupe"]}, "froid") == 1  # neutre
    assert client._meteo_rank({"tags": ["Plat froid"]}, "") == 1   # météo neutre


def test_slot_score_moment_quasi_bloquant():
    soir_only = {"soir"}
    # une recette « Soir » est lourdement pénalisée sur un créneau midi
    assert client._slot_score(soir_only, "midi", 1) > 50
    assert client._slot_score(soir_only, "soir", 1) < 0
    # léger mieux noté le soir ; copieux mieux le midi
    assert client._slot_score({"leger"}, "soir", 1) < client._slot_score({"leger"}, "midi", 1)
    assert client._slot_score({"copieux"}, "midi", 1) < client._slot_score({"copieux"}, "soir", 1)
    # mijoté favorisé le week-end (jour 6) vs semaine (jour 2)
    assert client._slot_score({"mijote"}, "soir", 6) < client._slot_score({"mijote"}, "soir", 2)


def test_assign_slots_respects_moment_tag():
    # « Soir uniquement » ne doit pas atterrir en midi si d'autres choix existent
    meta = {
        "poulet soir": {"nom": "Poulet soir", "tags": ["Soir"], "repas": "Plat"},
        "midi a": {"nom": "Midi A", "tags": [], "repas": "Plat"},
        "midi b": {"nom": "Midi B", "tags": [], "repas": "Plat"},
    }
    names = ["Poulet soir", "Midi A", "Midi B"]
    plats = client._assign_slots(names, [1, 1, 1, 1, 1, 1, 1], [1], meta)
    midis = {p["nom_recette"] for p in plats if p["moment"] == "midi"}
    soirs = {p["nom_recette"] for p in plats if p["moment"] == "soir"}
    assert "Poulet soir" not in midis
    assert "Poulet soir" in soirs


def test_assign_slots_skips_off_meals():
    groups = [1, 1, 2, 2, 2, 3, 4]
    unique = [1, 2, 3, 4]
    names = ["A", "B", "C", "D", "S1", "S2", "S3", "S4", "S5", "S6", "S7"]
    # Lundi midi + Dimanche soir désactivés
    off = {(1, "midi"), (7, "soir")}
    plats = client._assign_slots(names, groups, unique, {}, [], off)
    slots = {(p["jour"], p["moment"]) for p in plats}
    assert (1, "midi") not in slots
    assert (7, "soir") not in slots
    # Mardi midi (même groupe que lundi) reste présent
    assert (2, "midi") in slots
    assert (1, "soir") in slots


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
