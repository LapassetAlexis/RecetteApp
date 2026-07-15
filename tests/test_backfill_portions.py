"""Tests du backfill des Portions (logique pure, sans réseau)."""

import json

from scripts.backfill_portions import _yield_from_html, parse_yield


def test_parse_yield_cases():
    # Chaînes avec unités / texte français.
    assert parse_yield("4") == 4
    assert parse_yield("4 portions") == 4
    assert parse_yield("Pour 6 personnes") == 6
    # Nombres bruts.
    assert parse_yield(4) == 4
    assert parse_yield(4.0) == 4
    # Listes : 1er élément exploitable.
    assert parse_yield(["4"]) == 4
    assert parse_yield(["", "Pour 8 personnes"]) == 8
    # Plage "4-6" → prend le 1er entier.
    assert parse_yield("4-6") == 4
    # Rien d'exploitable → None (on laisse le défaut 4 ailleurs).
    assert parse_yield("") is None
    assert parse_yield(None) is None
    assert parse_yield("quelques") is None
    assert parse_yield([]) is None
    # Hors plage plausible 1..12 → None.
    assert parse_yield("0") is None
    assert parse_yield("50 g") is None
    # bool n'est pas un nombre de portions.
    assert parse_yield(True) is None


def test_yield_from_html_jsonld():
    node = {"@context": "https://schema.org", "@type": "Recipe",
            "name": "Test", "recipeYield": "6 portions"}
    html = f'<script type="application/ld+json">{json.dumps(node)}</script>'
    assert parse_yield(_yield_from_html(html)) == 6


def test_yield_from_html_absent():
    node = {"@context": "https://schema.org", "@type": "Recipe", "name": "Sans yield"}
    html = f'<script type="application/ld+json">{json.dumps(node)}</script>'
    assert _yield_from_html(html) is None
    assert _yield_from_html("") is None
    assert _yield_from_html("<html>pas de jsonld</html>") is None
