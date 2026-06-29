"""Tests du parseur de planning LLM (_parse_planning)."""

import pytest

from app.llm_client import LLMClient

# Instancier en mode ollama (défaut) : aucune connexion réseau au __init__.
client = LLMClient()


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
