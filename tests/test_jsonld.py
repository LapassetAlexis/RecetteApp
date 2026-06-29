"""Tests de l'extraction JSON-LD schema.org (sans réseau ni LLM)."""

from app.llm_client import (
    _extract_jsonld_recipe,
    _first_image,
    _flatten_instructions,
    _handler_amandinecooking,
    _ingredients_from_text,
    _scrape_ingredient_list,
    _split_blob,
    _join_fragments,
)


def test_join_fragments_reassembles_sentences():
    frags = ["Épluchez l'oignon", "l'ail et les carottes puis coupez-les.", "Cuire 45 min."]
    assert _join_fragments(frags) == [
        "Épluchez l'oignon, l'ail et les carottes puis coupez-les.",
        "Cuire 45 min.",
    ]


def test_flatten_skips_garbage_name():
    from app.llm_client import _flatten_instructions
    val = [
        {"@type": "HowToStep", "text": "", "name": "Array,Array,Array,Array"},
        {"@type": "HowToStep", "text": "Cuire 10 minutes."},
    ]
    assert _flatten_instructions(val) == ["Cuire 10 minutes."]


def test_split_blob_ingredients_and_instructions():
    blob = ("Voici la recette\r\nIngrédients pour 4 personnes : - 5 pts / personne -\r\n"
            "\t250 g de lentilles vertes\r\n\t200 g de lardons\r\n\tSel et poivre.\r\n"
            "Préparation :\r\nÉpluchez l'oignon.\r\nCuire 45 minutes.")
    ings, instrs = _split_blob(blob)
    assert ings == ["250 g de lentilles vertes", "200 g de lardons", "Sel et poivre."]
    assert instrs == ["Épluchez l'oignon.", "Cuire 45 minutes."]


def test_scrape_ingredient_list_keeps_real_list():
    html = "<h2>Ingrédients</h2><ul><li>200 g farine</li><li>3 oeufs</li><li>1 pincée de sel</li></ul>"
    assert _scrape_ingredient_list(html) == ["200 g farine", "3 oeufs", "1 pincée de sel"]


def test_scrape_ingredient_list_ignores_nav():
    # un menu de navigation ne ressemble pas à des ingrédients -> rejeté
    html = "<p>Ingrédients</p><ul><li>Accueil</li><li>Contact</li><li>Blog</li></ul>"
    assert _scrape_ingredient_list(html) == []


def test_ingredients_from_text_blob():
    # cas platetrecette : ingrédients en blob texte (séparés \n / \t)
    blob = ("Voici la recette ...\nIngrédients pour 4 personnes : 3 SP / personne -\n"
            "\t700 g d'épinards\n\t4 œufs\n\t30 g de parmesan\nPréparation :\nLavez et hachez...")
    r = _ingredients_from_text(blob)
    assert r == ["700 g d'épinards", "4 œufs", "30 g de parmesan"]


def test_ingredients_from_text_none():
    assert _ingredients_from_text("Texte sans liste d'ingrédients") == []


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


def test_inline_object_without_ldjson_script():
    # Cas Marmiton : objet schema.org présent mais l'attribut type du <script>
    # est encodé en entités HTML, donc le regex sur la balise ne matche pas.
    # Le repli par {"@context"} + équilibrage d'accolades doit le retrouver.
    html = (
        'bla bla <script type="application&#x2F;ld&#x2B;json">'
        '{"@context":"http://schema.org","@type":"Recipe","name":"Lasagnes",'
        '"recipeIngredient":["500 g de boeuf","lait"],'
        '"recipeInstructions":[{"@type":"HowToStep","text":"Cuire la viande {voir note}."}]}'
        '</script> fin'
    )
    r = _extract_jsonld_recipe(html)
    assert r is not None and r["nom"] == "Lasagnes"
    assert r["ingredients"] == ["500 g de boeuf", "lait"]
    # le '}' dans la chaîne ne doit pas casser l'équilibrage
    assert "Cuire la viande {voir note}." in r["instructions"]


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


def test_recipe_root_with_graph_sibling():
    # Cas Ricardo : la racine EST un Recipe mais porte aussi un @graph (WebSite).
    # Il ne faut pas perdre le Recipe en ne regardant que @graph.
    html = """
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Recipe","name":"Nougat",
     "recipeIngredient":["sucre","miel"],"recipeInstructions":"Cuire.",
     "@graph":[{"@type":"WebSite","name":"site"}]}
    </script>
    """
    r = _extract_jsonld_recipe(html)
    assert r is not None and r["nom"] == "Nougat"
    assert r["ingredients"] == ["sucre", "miel"]


def test_ingredient_header_noise_filtered():
    # CuisineActuelle met un en-tête "Ingrédients" dans le tableau recipeIngredient.
    html = """
    <script type="application/ld+json">
    {"@type":"Recipe","name":"Galettes","recipeIngredient":["Ingr&eacute;dients","1 courgette","2 oeufs"],
     "recipeInstructions":"Cuire."}
    </script>
    """
    r = _extract_jsonld_recipe(html)
    assert r["ingredients"] == ["1 courgette", "2 oeufs"]


def test_handler_amandinecooking():
    html = """
    <title>Salade de lentilles - Amandine Cooking</title>
    <h2><span>Salade de lentilles aux crudités</span></h2>
    <p>intro</p>
    <h2><span>Ingr&eacute;dients pour 4 personnes</span></h2>
    <ul>
      <li><div><span>300g de lentilles s&egrave;ches</span></div></li>
      <li><div><span>1 oignon rouge</span></div></li>
    </ul>
    <h2><span>Pr&eacute;paration</span></h2>
    <ol>
      <li><div><span>Cuire les lentilles.</span></div></li>
      <li><div><span>M&eacute;langer le tout.</span></div></li>
    </ol>
    """
    h = _handler_amandinecooking(html)
    assert h is not None
    assert h["nom"] == "Salade de lentilles aux crudités"
    assert h["ingredients"] == ["300g de lentilles sèches", "1 oignon rouge"]
    assert "Cuire les lentilles." in h["instructions"]
    assert "Mélanger le tout." in h["instructions"]


def test_handler_amandinecooking_no_ingredients_returns_none():
    assert _handler_amandinecooking("<h2>rien</h2>") is None


def test_site_handler_registry():
    # On passe par le module vivant (et non par les noms importés au top) pour
    # rester correct même si un autre test a rechargé app.llm_client (reload).
    import app.llm_client as L

    def _dummy(html_text):
        return {"nom": "X", "ingredients": ["a"], "instructions": "", "image_url": "", "keywords": []}
    L.SITE_HANDLERS["exemple-test.com"] = _dummy
    try:
        assert L._site_handler("https://www.exemple-test.com/r/1") is _dummy
        assert L._site_handler("http://exemple-test.com/x") is _dummy
        assert L._site_handler("https://autre.com/x") is None
    finally:
        del L.SITE_HANDLERS["exemple-test.com"]
