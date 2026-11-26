"""M05 — créneaux vides de la page planning (1280 et 390 px), piège à focus
des modales (« Changer », picker, menu ⋯), couleurs et légende des groupes,
libellés et date par défaut du constructeur."""

import re
from datetime import date

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.outils import (
    cellule,
    choisir_plat,
    pas_de_defilement_horizontal,
    periode,
    semaine,
    semaine_validee,
)

pytestmark = pytest.mark.e2e


def _trois_jours_quatre_vides(page: Page, server, week_start="2026-11-11") -> int:
    """3 jours : midi 1 et soir 2 remplis → 4 créneaux vides."""
    return semaine_validee(page, server, semaine(3, {"1:midi": "p1", "2:soir": "c1"}), week_start)


def _focus_reste_dans(page: Page, selecteur: str, n: int = 30, shift: bool = False) -> None:
    for _ in range(n):
        page.keyboard.press("Shift+Tab" if shift else "Tab")
        assert page.evaluate(
            "s => document.querySelector(s).contains(document.activeElement)", selecteur
        ), f"focus sorti de {selecteur}"


@pytest.fixture
def mobile(browser: Browser):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                              locale="fr-FR", service_workers="block")
    page = ctx.new_page()
    erreurs: list[str] = []
    page.on("pageerror", lambda e: erreurs.append(str(e)))
    page.on("console", lambda m: erreurs.append(m.text) if m.type == "error" else None)
    yield page
    ctx.close()
    assert not erreurs, erreurs


def test_creneau_vide_desktop_ajout_et_piege_a_focus(page: Page, server):
    pid = _trois_jours_quatre_vides(page, server)
    page.goto(f"{server}/planning/{pid}")
    ajouts = page.locator(".week-grid .meal-add")
    expect(ajouts).to_have_count(4)

    page.locator('.week-grid .meal-add[data-jour="3"][data-moment="midi"]').click()
    expect(page.locator("#modal")).to_be_visible()
    expect(page.locator("#modal h3")).to_have_text("＋ Ajouter un repas")
    expect(page.locator("#brique-box")).to_be_hidden()
    expect(page.locator("#alt-list .alt").first).to_be_visible()
    _focus_reste_dans(page, "#modal")
    _focus_reste_dans(page, "#modal", n=10, shift=True)

    # Échap ferme et rend le focus au bouton « Ajouter ».
    page.keyboard.press("Escape")
    expect(page.locator("#modal")).to_be_hidden()
    assert page.evaluate("() => document.activeElement.classList.contains('meal-add')")

    page.locator('.week-grid .meal-add[data-jour="3"][data-moment="midi"]').click()
    with page.expect_request(lambda r: r.url.endswith("/meals")) as req:
        page.get_by_role("button", name="Choisir Gratin dauphinois").click()
    corps = req.value.post_data_json
    assert corps["action"] == "set_main" and corps["slots"] == [{"jour": 3, "moment": "midi"}]
    page.wait_for_load_state("load")
    expect(page.locator('.week-grid .meal.midi[data-jours="3"] .meal-name')).to_have_text("Gratin dauphinois")
    expect(ajouts).to_have_count(3)


def test_creneau_vide_mobile_repas_libre(mobile: Page, server):
    pid = _trois_jours_quatre_vides(mobile, server, "2026-11-04")
    mobile.goto(f"{server}/planning/{pid}")
    ajouts = mobile.locator(".week-list .meal-add")
    expect(ajouts).to_have_count(4)
    for i in range(4):
        expect(ajouts.nth(i)).to_be_visible()
        assert ajouts.nth(i).bounding_box()["height"] >= 44
    pas_de_defilement_horizontal(mobile)

    mobile.locator('.week-list .meal-add[data-jour="1"][data-moment="soir"]').click()
    expect(mobile.locator("#modal")).to_be_visible()
    mobile.click("#free-toggle")
    expect(mobile.locator("#free-ings")).to_be_hidden()
    mobile.fill("#free-nom", "Pizza maison")
    with mobile.expect_request(lambda r: r.url.endswith("/meals")) as req:
        mobile.click("#free-go")
    assert req.value.post_data_json["action"] == "set_main"
    mobile.wait_for_load_state("load")
    expect(mobile.locator(".week-list .meal.soir").filter(has_text="Pizza maison")).to_have_count(1)
    expect(ajouts).to_have_count(3)


def test_picker_et_menu_piege_a_focus(page: Page, server):
    page.goto(server + "/")
    periode(page, "2026-12-07", "2026-12-09")
    bouton = cellule(page, "1:soir").locator('[data-act="pick-main"]')
    bouton.click()
    expect(page.locator("#pk")).to_have_class(re.compile(r"\bopen\b"))
    _focus_reste_dans(page, "#pk", n=40)
    page.keyboard.press("Escape")
    expect(page.locator("#pk")).not_to_have_class(re.compile(r"\bopen\b"))
    expect(bouton).to_be_focused()

    choisir_plat(page, "1:soir", "Soupe de potiron")
    plus = cellule(page, "1:soir").locator(".b-more")
    plus.click()
    expect(page.locator(".b-menu")).to_be_visible()
    _focus_reste_dans(page, ".b-menu", n=8)
    page.keyboard.press("Escape")
    expect(page.locator(".b-menu")).to_have_count(0)
    expect(plus).to_be_focused()
    expect(plus).to_have_attribute("aria-expanded", "false")


def test_groupes_sept_couleurs_et_legende_de_la_periode(page: Page, server):
    page.goto(server + "/")
    # 4 jours du mercredi au samedi : la légende ne cite aucun autre jour.
    periode(page, "2026-12-09", "2026-12-12")
    legende = page.locator("#group-legend")
    expect(legende).to_contain_text("Mer")
    texte = legende.text_content()
    for jour in ("Dim", "Lun", "Mar"):
        assert jour not in texte, texte

    # Semaine pleine, un groupe par midi : 7 couleurs différentes.
    periode(page, "2026-12-07", "2026-12-13")
    for j in range(1, 8):
        cellule(page, f"{j}:midi").locator(".b-group").click()
        expect(page.locator(".group-sw")).to_have_count(7)
        page.locator(".group-sw").nth(j - 1).click()
    couleurs = page.locator(".b-group").evaluate_all(
        "els => els.map(e => getComputedStyle(e).backgroundColor)")
    assert len(couleurs) == 7 and len(set(couleurs)) == 7, couleurs
    expect(legende.locator(".gl-item")).to_have_count(7)


def test_constructeur_libelles_et_date_du_jour(page: Page, server):
    page.goto(server + "/")
    expect(page.locator("#week_start")).to_have_value(date.today().isoformat())
    expect(page.locator("#btn-b")).to_have_text("📅 Enregistrer et voir le planning")
    expect(page.locator(".edit-banner")).to_have_count(0)

    pid = _trois_jours_quatre_vides(page, server, "2026-10-07")
    page.goto(f"{server}/?planning={pid}")
    expect(page.locator(".edit-banner")).to_contain_text("semaine validée du 07/10/2026")
    expect(page.locator("#week_start")).to_have_value("2026-10-07")   # date gardée
    expect(page.locator("#btn-b")).to_have_text("✅ Enregistrer les modifications")
    page.click("#btn-cancel")
    page.wait_for_url(f"{server}/planning/{pid}")
