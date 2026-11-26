"""Restes du socle front (M11), en 1280 et 390 px :
- enrichissement de masse : confirmation par modale, POST lu en flux (fetch) ;
- « Ré-extraire » : confirmation par modale ;
- modale « Changer » : message lisible si Notion échoue (Common.requestJson) ;
- pastilles de groupe du constructeur : texte blanc à 4,5:1 au moins."""

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.outils import cellule, pas_de_defilement_horizontal, semaine, semaine_validee
from tests.test_jetons_contraste import contraste

pytestmark = pytest.mark.e2e

LARGEURS = [1280, 390]


@pytest.fixture(params=LARGEURS, ids=lambda w: f"{w}px")
def largeur(request, page: Page) -> int:
    page.set_viewport_size({"width": request.param, "height": 900 if request.param > 500 else 844})
    return request.param


def test_enrichissement_de_masse_modale_puis_flux(page: Page, server, largeur):
    page.goto(server + "/recettes")
    modale = page.locator("#enrich-modal")
    requetes = []
    page.on("request", lambda r: "/api/enrich-all/stream" in r.url and requetes.append(r.method))

    # Annuler (Échap) : rien n'est lancé, le focus revient au bouton.
    page.click("#btn-enrich")
    expect(modale).to_be_visible()
    expect(modale.get_by_role("button", name="Annuler")).to_be_focused()
    pas_de_defilement_horizontal(page)
    page.keyboard.press("Escape")
    expect(modale).to_be_hidden()
    expect(page.locator("#btn-enrich")).to_be_focused()
    assert requetes == []

    # Confirmer : POST lu en flux jusqu'à l'événement « done ».
    page.click("#btn-enrich")
    with page.expect_response(lambda r: "/api/enrich-all/stream" in r.url) as rep:
        modale.get_by_role("button", name="Enrichir").click()
    assert rep.value.request.method == "POST"
    expect(modale).to_be_hidden()
    expect(page.locator("#enrich-status")).to_have_text(re.compile(r"^✅ \d+/\d+ enrichies"))
    expect(page.locator("#btn-enrich")).to_be_enabled()
    assert requetes == ["POST"]


def test_enrichissement_de_masse_erreur_generique(page: Page, server, largeur):
    flux = ('data: {"total": 3, "done": 0}\n\n'
            'data: {"total": 3, "done": 1, "enriched": 1, "skipped": 0, "errors": 0}\n\n'
            'event: error\ndata: {"error": "Erreur interne, réessaie dans un instant."}\n\n')
    page.route("**/api/enrich-all/stream",
               lambda route: route.fulfill(status=200, content_type="text/event-stream", body=flux))
    page.goto(server + "/recettes")
    page.click("#btn-enrich")
    page.locator("#enrich-modal").get_by_role("button", name="Enrichir").click()
    expect(page.locator("#enrich-status")).to_have_text("❌ Erreur interne, réessaie dans un instant.")
    expect(page.locator("#btn-enrich")).to_be_enabled()


def test_reextraire_confirme_par_modale(page: Page, server, largeur):
    page.route("**/recette/p1/enrichir?refetch=1**",
               lambda route: route.fulfill(status=200, content_type="text/html", body="<p>extrait</p>"))
    page.goto(server + "/recette/p1/enrichir")
    page.fill("#source_url", "https://exemple.test/poulet")
    modale = page.locator("#extract-modal")

    page.click("#btn-extract")
    expect(modale).to_be_visible()
    pas_de_defilement_horizontal(page)
    modale.get_by_role("button", name="Annuler").click()
    expect(modale).to_be_hidden()
    expect(page).to_have_url(server + "/recette/p1/enrichir")

    page.click("#btn-extract")
    modale.get_by_role("button", name="Ré-extraire").click()
    expect(page).to_have_url(re.compile(r"/recette/p1/enrichir\?refetch=1&url=https%3A%2F%2Fexemple\.test%2Fpoulet$"))


def test_modale_changer_si_notion_echoue(page: Page, server, largeur, erreurs_console):
    pid = semaine_validee(page, server, semaine(1, {"1:midi": "p1", "1:soir": "c1"}), "2026-11-02")
    page.route("**/api/alternatives/**", lambda route: route.fulfill(
        status=502, content_type="application/json",
        body=json.dumps({"error": "Notion est injoignable, réessaie plus tard."})))
    page.goto(f"{server}/planning/{pid}")
    page.locator(".meal-ch:visible").first.click()
    expect(page.locator("#modal")).to_be_visible()
    alerte = page.locator("#alt-list [role=alert]")
    expect(alerte).to_have_text("⚠️ Notion est injoignable, réessaie plus tard.")
    pas_de_defilement_horizontal(page)
    # Le 502 simulé est journalisé par le navigateur (« Failed to load resource ») :
    # c'est la réponse attendue, pas une erreur de la page.
    erreurs_console[:] = [e for e in erreurs_console if "502" not in e]


def _rgb(css: str) -> str:
    r, g, b = (int(x) for x in re.findall(r"\d+", css)[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


def test_pastilles_de_groupe_lisibles(page: Page, server, largeur):
    page.goto(server + "/")
    pastille = cellule(page, "1:midi").locator(".b-group")
    expect(pastille).to_be_visible()
    couleurs = page.evaluate("window.Builder.GROUP_COLORS")
    assert len(couleurs) == 7
    for i, fond in enumerate(couleurs, start=1):
        # Chaque couleur de la palette posée sur la pastille, texte calculé par le CSS.
        styles = pastille.evaluate("(el, c) => { el.style.background = c; const s = getComputedStyle(el);"
                                   " return [s.color, s.backgroundColor]; }", fond)
        texte, rendu = _rgb(styles[0]), _rgb(styles[1])
        assert rendu == fond.lower(), (i, rendu, fond)
        assert contraste(texte, rendu) >= 4.5, f"G{i} : {texte} sur {rendu} = {contraste(texte, rendu):.2f}:1"
    pas_de_defilement_horizontal(page)
