"""Parcours recettes (M08) : liste (recherche, filtres, tri, notation),
ajout manuel, fiche (portions, suppression par modale commune) et modale
« Ajouter au planning » au focus piégé. JS sorti en fichiers."""

import re

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.outils import pas_de_defilement_horizontal

pytestmark = pytest.mark.e2e


def _visibles(page: Page) -> list[str]:
    return page.locator(".recipe-card:not(.hidden) .recipe-card-link").all_inner_texts()


def test_liste_recherche_filtres_tri_et_notation(page: Page, server):
    page.goto(server + "/recettes")
    # Par défaut : les briques (Ingrédient) sont masquées.
    riz = page.locator('.recipe-card[data-name="riz"]')
    expect(riz).to_be_hidden()
    expect(page.locator(".recipe-card:not(.hidden)").first).to_be_visible()

    # Recherche par ingrédient (temporisée).
    page.fill("#recipe-search", "lentilles")
    expect(page.locator(".recipe-card:not(.hidden) .recipe-card-link").first).to_have_text("Curry de lentilles")
    assert "Poulet rôti" not in _visibles(page)
    page.fill("#recipe-search", "zzz introuvable")
    expect(page.locator("#no-result")).to_be_visible()
    page.fill("#recipe-search", "")
    expect(page.locator("#no-result")).to_be_hidden()

    # Filtre « Toutes » : la brique Riz apparaît ; filtre de type Dessert.
    page.locator('.stat-chip[data-filter="nature"][data-value=""]').click()
    expect(riz).to_be_visible()
    dessert = page.locator('.stat-chip[data-filter="type"][data-value="Dessert"]')
    dessert.click()
    expect(dessert).to_have_attribute("aria-pressed", "true")
    expect(riz).to_be_hidden()
    assert "Tarte aux pommes" in _visibles(page) and "Poulet rôti" not in _visibles(page)
    page.locator('.stat-chip[data-filter="type"][data-value=""]').click()

    # Tri par nom.
    page.select_option("#recipe-sort", "nom")
    noms = page.evaluate("""() => Array.from(document.querySelectorAll('.recipe-card:not(.hidden)'))
        .map(c => c.dataset.name)""")
    attendu = page.evaluate("n => [...n].sort((a, b) => a.localeCompare(b, 'fr'))", noms)
    assert noms == attendu and noms[0] == "bœuf bourguignon"

    # Notation : clic sur la 4e étoile de « Quiche lorraine ».
    etoiles = page.locator('.recipe-stars[data-id="q1"]')
    with page.expect_response(lambda r: r.url.endswith("/api/rate/q1")) as rep:
        etoiles.locator('.star[data-val="4"]').click()
    assert rep.value.json()["success"]
    expect(etoiles.locator('.star[data-val="4"]')).to_have_attribute("aria-pressed", "true")
    expect(etoiles.locator(".star-clear")).to_be_visible()


def test_ajout_manuel_puis_suppression_par_modale(page: Page, server):
    page.goto(server + "/ajouter")
    page.click("#btn-manual")
    expect(page.locator("#step2")).to_be_visible()
    expect(page.locator("#nom")).to_be_focused()
    page.fill("#nom", "Omelette test M08")
    page.fill("#ingredients_manual", "3 oeufs\nsel, poivre")
    page.locator("#steps textarea").first.fill("Battre les œufs.")
    page.click("#btn-add")
    expect(page.locator(".alert-success")).to_contain_text("Omelette test M08")

    page.get_by_role("link", name="Voir la recette").click()
    page.wait_for_url(re.compile(r"/recette/new-"))
    # « sel, poivre » découpé en deux ingrédients.
    expect(page.locator(".ing .ing-n")).to_have_text(["oeufs", "sel", "poivre"])

    # Portions : 4 → 5, le lien du mode cuisine suit.
    page.click("#srv-plus")
    expect(page.locator("#srv-n")).to_have_text("5")
    expect(page.locator("#btn-cuisine")).to_have_attribute("href", re.compile(r"portions=5$"))

    # Modale de suppression : focus piégé, Échap, puis confirmation.
    page.click("#btn-delete")
    expect(page.locator("#del-modal")).to_be_visible()
    expect(page.locator("#del-cancel")).to_be_focused()
    for _ in range(4):
        page.keyboard.press("Tab")
        assert page.evaluate("() => document.getElementById('del-modal').contains(document.activeElement)")
    page.keyboard.press("Escape")
    expect(page.locator("#del-modal")).to_be_hidden()
    expect(page.locator("#btn-delete")).to_be_focused()
    page.click("#btn-delete")
    page.click("#del-confirm")
    page.wait_for_url(server + "/recettes")
    expect(page.locator(".recipe-card-link", has_text="Omelette test M08")).to_have_count(0)


def test_modale_ajouter_au_planning_piege_le_focus(page: Page, server):
    page.goto(server + "/recette/s1")
    page.click("#btn-planifier")
    expect(page.locator("#plan-modal")).to_be_visible()
    expect(page.locator("#plan-draft")).to_be_focused()
    for _ in range(8):
        page.keyboard.press("Shift+Tab")
        assert page.evaluate("() => document.getElementById('plan-modal').contains(document.activeElement)")
    page.keyboard.press("Escape")
    expect(page.locator("#plan-modal")).to_be_hidden()
    expect(page.locator("#btn-planifier")).to_be_focused()


def test_pages_recettes_en_390_sans_debordement(browser: Browser, server):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                              locale="fr-FR", service_workers="block")
    page = ctx.new_page()
    erreurs: list[str] = []
    page.on("pageerror", lambda e: erreurs.append(str(e)))
    page.on("console", lambda m: erreurs.append(m.text) if m.type == "error" else None)
    try:
        for chemin in ("/recettes", "/recette/p1", "/recette/p1/enrichir", "/ajouter"):
            page.goto(server + chemin)
            expect(page.locator("h1").first).to_be_visible()
            pas_de_defilement_horizontal(page)
        page.goto(server + "/recette/p1")
        page.click("#btn-planifier")
        expect(page.locator("#plan-modal")).to_be_visible()
        pas_de_defilement_horizontal(page)
    finally:
        ctx.close()
    assert not erreurs, erreurs
