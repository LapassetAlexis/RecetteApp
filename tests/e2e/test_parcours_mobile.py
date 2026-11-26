"""Affichage mobile (390 × 844) : aucun défilement horizontal sur le
constructeur, la page planning et la liste de courses publique."""

import pytest
from playwright.sync_api import Browser, expect

from tests.e2e.outils import pas_de_defilement_horizontal, semaine, semaine_validee

pytestmark = pytest.mark.e2e


@pytest.fixture
def mobile(browser: Browser):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                              locale="fr-FR", service_workers="block")
    page = ctx.new_page()
    erreurs: list[str] = []
    page.on("pageerror", lambda e: erreurs.append(str(e)))
    yield page
    ctx.close()
    assert not erreurs, erreurs


def test_pas_de_defilement_horizontal_en_390(mobile, server):
    cases = semaine(4, {"1:midi": ("x1", 1), "2:midi": (None, 1), "1:soir": "b1", "2:soir": "q1",
                        "3:midi": "h1", "4:soir": "g1"})
    pid = semaine_validee(mobile, server, cases, "2026-10-21")

    mobile.goto(server + "/")
    expect(mobile.locator(".b-rowlabel").first).to_be_visible()
    pas_de_defilement_horizontal(mobile)

    mobile.goto(f"{server}/?planning={pid}")
    expect(mobile.locator('.b-cell[data-key="1:midi"] .b-main')).to_be_visible()
    pas_de_defilement_horizontal(mobile)

    mobile.goto(f"{server}/planning/{pid}")
    expect(mobile.locator(".week-list")).to_be_visible()
    expect(mobile.locator(".week-grid")).to_be_hidden()
    pas_de_defilement_horizontal(mobile)

    lien = mobile.request.post(f"{server}/planning/{pid}/partager").json()["url"]
    mobile.goto(server + lien)
    expect(mobile.locator("#shop-g")).to_be_visible()
    pas_de_defilement_horizontal(mobile)
