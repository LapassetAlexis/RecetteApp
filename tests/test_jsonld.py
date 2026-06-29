"""Tests de l'extraction JSON-LD schema.org (sans réseau ni LLM)."""

from app.llm_client import _extract_jsonld_recipe, _first_image, _flatten_instructions


def test_first_image_variants():
    assert _first_image("http://x/a.jpg") == "http://x/a.jpg"
    assert _first_image({"url": "http://x/b.jpg"}) == "http://x/b.jpg"
    assert _first_image(["http://x/c.jpg", "http://x/d.jpg"]) == "http://x/c.jpg"
    assert _first_image([{"url": "http://x/e.jpg"}]) == "http://x/e.jpg"
    assert _first_image(None) == ""


def test_flatten_instructions_howtostep():
    val = [
        {"@type": "HowToStep", "text": "Préchauffer le four."},
        {"@type": "HowToStep", "text": "Mélanger la farine."},
    ]
    assert _flatten_instructions(val) == ["Préchauffer le four.", "Mélanger la farine."]


def test_flatten_instructions_section():
    val = [{
        "@type": "HowToSection",
        "itemListElement": [
            {"@type": "HowToStep", "text": "Étape 1"},
            {"@type": "HowToStep", "text": "Étape 2"},
        ],
    }]
    assert _flatten_instructions(val) == ["Étape 1", "Étape 2"]


def test_extract_recipe_basic():
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Recipe","name":"Tarte aux pommes",
     "image":{"url":"http://img/tarte.jpg"},
     "recipeIngredient":["3 pommes","200 g de farine","100 g de sucre"],
     "recipeInstructions":[{"@type":"HowToStep","text":"Éplucher les pommes."},
                           {"@type":"HowToStep","text":"Enfourner 30 min."}],
     "keywords":"dessert, pommes"}
    </script></head><body></body></html>
    """
    r = _extract_jsonld_recipe(html)
    assert r is not None
    assert r["nom"] == "Tarte aux pommes"
    assert r["image_url"] == "http://img/tarte.jpg"
    assert r["ingredients"] == ["3 pommes", "200 g de farine", "100 g de sucre"]
    assert "Éplucher les pommes." in r["instructions"]
    assert "Enfourner 30 min." in r["instructions"]
    assert "dessert" in r["keywords"]


def test_extract_recipe_in_graph():
    html = """
    <script type="application/ld+json">
    {"@context":"https://schema.org","@graph":[
       {"@type":"WebPage","name":"page"},
       {"@type":"Recipe","name":"Soupe","recipeIngredient":["eau","sel"],
        "recipeInstructions":"Faire bouillir."}
    ]}
    </script>
    """
    r = _extract_jsonld_recipe(html)
    assert r is not None and r["nom"] == "Soupe"
    assert r["ingredients"] == ["eau", "sel"]


def test_no_recipe_returns_none():
    html = '<script type="application/ld+json">{"@type":"WebPage"}</script>'
    assert _extract_jsonld_recipe(html) is None


def test_entities_decoded():
    html = """
    <script type="application/ld+json">
    {"@type":"Recipe","name":"Boeuf &amp; carottes","recipeIngredient":["1 c. &agrave; soupe d'huile"],
     "recipeInstructions":"Cuire."}
    </script>
    """
    r = _extract_jsonld_recipe(html)
    assert r["nom"] == "Boeuf & carottes"
    assert "à soupe" in r["ingredients"][0]
