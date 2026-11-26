"""Parcours « 📅 Ajouter au planning » depuis la fiche recette (L11) : dans
un brouillon existant (créneau libre, puis avertissement sur un créneau
occupé) et dans un nouveau brouillon."""

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.outils import cellule, construire, semaine

pytestmark = pytest.mark.e2e


def test_ajout_depuis_la_fiche_dans_un_brouillon(page: Page, server):
    cases = semaine(3, {"1:midi": "p1", "1:soir": "s1", "3:soir": "q1"})
    pid = construire(page, server, cases, "2026-11-04", action="brouillon")  # un mercredi

    page.goto(f"{server}/recette/c1")
    page.click("#btn-planifier")
    expect(page.locator("#plan-modal")).to_be_visible()
    page.select_option("#plan-draft", str(pid))
    expect(page.locator("#plan-day-g")).to_be_visible()
    expect(page.locator("#plan-day option").first).to_have_text("Mercredi 04/11")

    # Créneau occupé : avertissement de remplacement.
    page.select_option("#plan-day", "1")
    page.select_option("#plan-moment", "soir")
    expect(page.locator("#plan-warn")).to_have_text("Ce créneau contient « Soupe de potiron » : il sera remplacé.")
    # Créneau libre : pas d'avertissement.
    page.select_option("#plan-day", "2")
    expect(page.locator("#plan-warn")).to_be_hidden()

    with page.expect_response(lambda r: r.url.endswith("/api/recette/c1/planifier")) as rep:
        page.click("#plan-ok")
    assert rep.value.json()["success"]
    expect(page.locator("#plan-done")).to_be_visible()
    # La modale connaît maintenant le créneau occupé (second ajout).
    expect(page.locator("#plan-warn")).to_have_text(re.compile("Curry de lentilles"))

    page.click("#plan-done-link")
    page.wait_for_url(f"{server}/?draft={pid}")
    expect(page.locator(".b-rowlabel")).to_have_count(3)
    expect(cellule(page, "2:soir").locator(".b-main")).to_contain_text("Curry de lentilles")
    expect(cellule(page, "1:soir").locator(".b-main")).to_contain_text("Soupe de potiron")


def test_ajout_depuis_la_fiche_nouveau_brouillon(page: Page, server):
    page.goto(f"{server}/recette/g1")
    page.click("#btn-planifier")
    page.select_option("#plan-draft", "new")
    expect(page.locator("#plan-date-g")).to_be_visible()
    expect(page.locator("#plan-day-g")).to_be_hidden()
    page.fill("#plan-date", "2026-12-10")
    page.select_option("#plan-moment", "midi")
    with page.expect_response(lambda r: r.url.endswith("/api/recette/g1/planifier")) as rep:
        page.click("#plan-ok")
    nouveau = rep.value.json()["planning_id"]
    expect(page.locator("#plan-done")).to_be_visible()

    page.click("#plan-done-link")
    page.wait_for_url(f"{server}/?draft={nouveau}")
    expect(page.locator("#week_start")).to_have_value("2026-12-10")
    expect(page.locator(".b-rowlabel")).to_have_text(["Jeu 10/12"])
    expect(cellule(page, "1:midi").locator(".b-main")).to_contain_text("Gratin dauphinois")


def test_fiche_et_mode_cuisine_virgule_decimale(page: Page, server):
    """Mise à l'échelle des portions sur la fiche (4 → 6 : 1 kg de poulet →
    « 1,5 kg ») puis mode cuisine pour 6 : virgule décimale partout."""
    page.goto(f"{server}/recette/p1")
    expect(page.locator("#srv-n")).to_have_text("4")
    page.click("#srv-plus")
    page.click("#srv-plus")
    poulet = page.locator(".ing").filter(has_text="poulet").locator(".ing-q")
    expect(poulet).to_have_text("1,5 kg")
    page.click("#btn-cuisine")
    page.wait_for_url(re.compile(r"/recette/p1/cuisine\?portions=6$"))
    expect(page.locator(".ck-ing-q").filter(has_text="kg")).to_have_text("1,5 kg")
