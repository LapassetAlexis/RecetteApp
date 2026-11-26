"""M07 — navigation : « Cette semaine » mène au planning en cours ; en 390 px
le menu tient sur deux lignes, chaque lien fait au moins 44 px de haut, sans
défilement horizontal ; bandeau de la page « Données » sans auth."""

import re
from datetime import date

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.outils import pas_de_defilement_horizontal, semaine, semaine_validee

pytestmark = pytest.mark.e2e


@pytest.fixture
def mobile(browser: Browser):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                              locale="fr-FR", service_workers="block")
    page = ctx.new_page()
    erreurs: list[str] = []
    page.on("pageerror", lambda e: erreurs.append(str(e)))
    page.on("console", lambda m: m.type == "error" and erreurs.append(m.text))
    yield page
    ctx.close()
    assert not erreurs, erreurs


def _planning_du_jour(page: Page, server: str) -> int:
    return semaine_validee(page, server, semaine(7, {"1:midi": "x1", "2:soir": "b1"}), date.today().isoformat())


def test_cette_semaine_depuis_le_menu(page: Page, server):
    pid = _planning_du_jour(page, server)
    page.goto(server + "/recettes")
    page.locator(".nav-links").get_by_role("link", name="Cette semaine").click()
    expect(page).to_have_url(re.compile(rf"/planning/{pid}$"))
    expect(page.locator(".week-grid")).to_be_visible()


def test_donnees_dans_le_menu_et_bandeau(page: Page, server):
    page.goto(server + "/")
    page.locator(".nav-links").get_by_role("link", name="Données").click()
    expect(page).to_have_url(re.compile(r"/admin$"))
    expect(page.locator(".nav-links a.active")).to_have_text("Données")
    expect(page.get_by_role("note")).to_contain_text("Accès non protégé")


def _mesures_menu(page: Page) -> dict:
    return page.evaluate("""() => {
        const els = [document.querySelector('.nav-brand'), ...document.querySelectorAll('.nav-links a')];
        const r = els.map(e => e.getBoundingClientRect());
        return {
            hauteurs: r.slice(1).map(b => Math.round(b.height)),
            largeurs: r.map(b => Math.round(b.width)),
            lignes: new Set(r.map(b => Math.round(b.top))).size,
        };
    }""")


@pytest.mark.parametrize("chemin", ["/", "/historique", "/admin", "/recettes"])
def test_menu_mobile_deux_lignes_cibles_44(mobile: Page, server, chemin):
    mobile.goto(server + chemin)
    expect(mobile.locator(".nav-links a").first).to_be_visible()
    m = _mesures_menu(mobile)
    assert min(m["hauteurs"]) >= 44, m
    assert min(m["largeurs"]) >= 44, m
    assert m["lignes"] <= 2, m
    pas_de_defilement_horizontal(mobile)


def test_cette_semaine_en_mobile(mobile: Page, server):
    pid = _planning_du_jour(mobile, server)
    mobile.goto(server + "/")
    mobile.locator(".nav-links").get_by_role("link", name="Cette semaine").click()
    expect(mobile).to_have_url(re.compile(rf"/planning/{pid}$"))
    expect(mobile.locator(".week-list")).to_be_visible()
    # cases à cocher des repas : zone cliquable de 44 px
    tailles = mobile.evaluate("""() => [...document.querySelectorAll('.week-list .meal-cb')]
        .map(l => { const b = l.getBoundingClientRect(); return Math.min(b.width, b.height); })""")
    assert tailles and min(tailles) >= 44, tailles
    pas_de_defilement_horizontal(mobile)
