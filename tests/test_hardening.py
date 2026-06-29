"""Tests des durcissements : anti-SSRF, classification lexicale, pipeline
d'extraction (sans réseau, fetch mocké). Pas de pytest-asyncio : on utilise
asyncio.run() pour rester sans dépendance de test supplémentaire."""

import asyncio

import pytest

from app import llm_client
from app.llm_client import LLMClient, _is_public_http_url, _safe_fetch_html

client = LLMClient()  # provider ollama par défaut : aucun appel réseau au init


# ── Anti-SSRF ────────────────────────────────────────────────
@pytest.mark.parametrize("url,expected", [
    ("https://www.marmiton.org/recette", True),
    ("http://localhost/x", False),
    ("http://127.0.0.1:5432", False),
    ("http://169.254.169.254/latest/meta-data/", False),
    ("http://192.168.1.10/admin", False),
    ("http://10.0.0.5", False),
    ("file:///etc/passwd", False),
    ("ftp://example.com", False),
    ("pas-une-url", False),
])
def test_is_public_http_url(url, expected):
    assert asyncio.run(_is_public_http_url(url)) is expected


def test_safe_fetch_rejects_internal():
    # URL interne -> "" sans même tenter de fetch réseau
    assert asyncio.run(_safe_fetch_html("http://127.0.0.1/secret")) == ""


# ── Classification lexicale ──────────────────────────────────
def test_keyword_guess_savory_sweet():
    t, _, sav, sw = client._keyword_guess("Cheesecake salé au saumon", ["saumon"], [])
    assert t == "Plat" and sav and not sw
    t, _, _, _ = client._keyword_guess("Tiramisu framboises", ["mascarpone", "sucre"], ["dessert"])
    assert t == "Dessert"
    t, _, _, _ = client._keyword_guess("Smoothie banane", ["banane"], ["boisson"])
    assert t == "Boisson"


def test_classify_corrects_savory_dessert(monkeypatch):
    # Le LLM renvoie 'Dessert' à tort pour un plat salé -> corrigé.
    async def _fake_chat(*a, **k):
        return '{"type_repas": "Dessert", "tags": []}'
    monkeypatch.setattr(client, "_chat", _fake_chat)
    t, _ = asyncio.run(client._classify_type_tags("Cheesecake salé au saumon", ["saumon fumé"], []))
    assert t != "Dessert"


def test_classify_fallback_on_llm_error(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("LLM down")
    monkeypatch.setattr(client, "_chat", _boom)
    t, tags = asyncio.run(client._classify_type_tags("Curry de lentilles", ["lentille", "curry"], ["plat"]))
    assert t == "Plat"  # repli heuristique, pas d'exception


# ── Pipeline extract_recipe_from_url (fetch mocké, sans réseau) ──
def test_extract_recipe_from_url_jsonld(monkeypatch):
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Recipe","name":"Tarte aux pommes",
     "recipeIngredient":["3 pommes","200 g de farine"],
     "recipeInstructions":[{"@type":"HowToStep","text":"Éplucher."},
                           {"@type":"HowToStep","text":"Enfourner."}]}
    </script></head><body></body></html>
    """

    async def _fake_fetch(url):
        return html

    async def _fake_classify(nom, ings, kw):
        return "Dessert", ["Quiche/tarte"]

    monkeypatch.setattr(llm_client, "_safe_fetch_html", _fake_fetch)
    monkeypatch.setattr(client, "_classify_type_tags", _fake_classify)
    r = asyncio.run(client.extract_recipe_from_url("https://exemple.test/tarte"))
    assert r["nom"] == "Tarte aux pommes"
    assert r["ingredients"] == ["3 pommes", "200 g de farine"]
    assert "Éplucher." in r["instructions"]
    assert r["source"] == "jsonld"


# ── Catégorisation des courses par rayon ─────────────────────
def test_categorize_and_group():
    from app.categories import categorize, group_by_rayon
    assert categorize("tomates cerises") == "Fruits & légumes"
    assert categorize("blanc de poulet") == "Viande & poisson"
    assert categorize("crème fraîche") == "Crémerie & œufs"
    assert categorize("pâtes complètes") == "Épicerie salée"
    assert categorize("xyz inconnu") == "Autre"
    g = group_by_rayon([{"nom": "tomate"}, {"nom": "poulet"}, {"nom": "zzz"}])
    rayons = [x["rayon"] for x in g]
    assert "Fruits & légumes" in rayons and "Viande & poisson" in rayons and "Autre" in rayons
    # ordre magasin : fruits/légumes avant viande
    assert rayons.index("Fruits & légumes") < rayons.index("Viande & poisson")
