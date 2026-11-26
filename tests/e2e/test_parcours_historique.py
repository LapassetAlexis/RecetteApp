"""Parcours de l'historique (M08) : dates lisibles, liens d'un brouillon,
aperçu déplié, suppression par modale commune (focus piégé, Échap) et
« Refaire » d'une semaine validée. Bureau et 390 px."""

import re

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.outils import construire, pas_de_defilement_horizontal, semaine, semaine_validee

pytestmark = pytest.mark.e2e


def _focus_dans(page: Page, selecteur: str) -> bool:
    return page.evaluate("s => document.querySelector(s).contains(document.activeElement)", selecteur)


def test_historique_brouillon_apercu_suppression_et_refaire(page: Page, server):
    brouillon = construire(page, server, semaine(2, {"1:midi": "p1", "2:soir": "s1"}),
                           "2026-09-02", action="brouillon")
    validee = semaine_validee(page, server, semaine(2, {"1:soir": "c1"}), "2026-09-09")

    page.goto(server + "/historique")
    carte_b = page.locator(f'.hist-item[data-card="{brouillon}"]')
    expect(carte_b.locator(".meta")).to_have_text(re.compile(r"^Créé le \d{2}/\d{2}/\d{4}$"))
    expect(carte_b.get_by_role("link", name=re.compile("Reprendre"))).to_have_attribute("href", f"/?draft={brouillon}")
    expect(carte_b.get_by_role("link", name=re.compile("Voir"))).to_have_attribute("href", f"/planning/{brouillon}")

    # Aperçu déplié dans la carte (bouton 👁).
    carte_b.locator(".hist-pv-btn").click()
    expect(carte_b.locator(".hist-pv")).to_contain_text("Poulet rôti")
    expect(carte_b.locator(".hist-pv-btn")).to_have_attribute("aria-expanded", "true")

    # Modale de suppression : focus initial sur Annuler, Tab piégé, Échap
    # ferme et rend le focus au bouton.
    supprimer = carte_b.locator(".hist-del")
    supprimer.click()
    modale = page.locator("#del-modal")
    expect(modale).to_be_visible()
    expect(page.locator("#del-msg")).to_contain_text("le brouillon")
    expect(page.locator("#del-cancel")).to_be_focused()
    for _ in range(5):
        page.keyboard.press("Tab")
        assert _focus_dans(page, "#del-modal")
    page.keyboard.press("Escape")
    expect(modale).to_be_hidden()
    expect(supprimer).to_be_focused()

    # Confirmation : la carte disparaît.
    supprimer.click()
    with page.expect_response(lambda r: r.url.endswith(f"/planning/{brouillon}/supprimer")):
        page.click("#del-ok")
    expect(carte_b).to_have_count(0)
    expect(modale).to_be_hidden()

    # « Refaire » une semaine validée : nouveau brouillon, page planning.
    page.locator(f'.hist-dup[data-id="{validee}"]').click()
    page.wait_for_url(re.compile(r"/planning/(\d+)$"))
    nouveau = int(page.url.rsplit("/", 1)[1])
    assert nouveau != validee
    expect(page.locator("body")).to_contain_text("Curry de lentilles")


def test_historique_en_390_sans_debordement(browser: Browser, server):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                              locale="fr-FR", service_workers="block")
    page = ctx.new_page()
    erreurs: list[str] = []
    page.on("pageerror", lambda e: erreurs.append(str(e)))
    page.on("console", lambda m: erreurs.append(m.text) if m.type == "error" else None)
    try:
        brouillon = construire(page, server, semaine(1, {"1:midi": "g1"}), "2026-09-16", action="brouillon")
        page.goto(server + "/historique")
        carte = page.locator(f'.hist-item[data-card="{brouillon}"]')
        expect(carte).to_be_visible()
        carte.locator(".hist-pv-btn").click()
        expect(carte.locator(".hist-pv")).to_contain_text("Gratin dauphinois")
        pas_de_defilement_horizontal(page)
        carte.locator(".hist-del").click()
        expect(page.locator("#del-modal")).to_be_visible()
        pas_de_defilement_horizontal(page)
        page.click("#del-cancel")
        expect(page.locator("#del-modal")).to_be_hidden()
    finally:
        ctx.close()
    assert not erreurs, erreurs
