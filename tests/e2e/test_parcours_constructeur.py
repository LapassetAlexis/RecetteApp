"""Parcours du constructeur de semaine (« / ») : période, groupes de midis,
brouillon, changement de date, validation, restes, réouverture.

Bugs couverts : B1 (date du brouillon), B2 et B3 (« Refaire » puis
« Reprendre »), B5 (jours hors période affichés « Absent »)."""

import re
from datetime import date, timedelta

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.outils import (
    cellule,
    choisir_plat,
    periode,
    prochain,
    semaine,
    semaine_validee,
)

pytestmark = pytest.mark.e2e


def _toast(page: Page) -> str:
    t = page.locator("#toast-zone .toast").first
    expect(t).to_be_visible()
    return t.text_content() or ""


def test_brouillon_puis_changement_de_date_puis_validation(page: Page, server):
    """4 jours (mercredi → samedi) avec un groupe de midis, brouillon, rouverture,
    nouvelle date de début (un mardi), validation : titre et jours suivent la
    nouvelle date (B1), aucun jour hors période « Absent » (B5)."""
    page.goto(server + "/")
    periode(page, "2026-11-25", "2026-11-28")
    expect(page.locator(".b-rowlabel")).to_have_text(["Mer 25/11", "Jeu 26/11", "Ven 27/11", "Sam 28/11"])

    # Groupes par défaut : G1 = mer + jeu midi, G2 = ven + sam midi.
    choisir_plat(page, "1:midi", "Poulet rôti")
    choisir_plat(page, "1:soir", "Curry de lentilles", recherche="curry")
    choisir_plat(page, "2:soir", "Soupe de potiron")
    choisir_plat(page, "3:midi", "Gratin dauphinois")
    choisir_plat(page, "3:soir", "Quiche lorraine")
    choisir_plat(page, "4:soir", "Bœuf bourguignon", recherche="bourguignon")
    expect(cellule(page, "2:midi").locator(".b-main.ro")).to_contain_text("Poulet rôti")
    expect(cellule(page, "2:midi").locator(".b-follow-note")).to_contain_text("même repas que Mer")

    page.click("#btn-draft")
    page.wait_for_url(re.compile(r"/\?draft=\d+&saved=1$"))
    pid = int(re.search(r"draft=(\d+)", page.url).group(1))
    assert "Brouillon enregistré" in _toast(page)

    # Brouillon rouvert : période, grille et groupes restaurés.
    expect(page.locator("#week_start")).to_have_value("2026-11-25")
    expect(page.locator("#week_end")).to_have_value("2026-11-28")
    expect(page.locator("#draft-id-input")).to_have_value(str(pid))
    expect(page.locator(".b-rowlabel")).to_have_count(4)
    expect(cellule(page, "3:midi").locator(".b-main")).to_contain_text("Gratin dauphinois")
    expect(cellule(page, "4:midi").locator(".b-main.ro")).to_contain_text("Gratin dauphinois")

    # Nouvelle date de début : la durée (4 jours) est gardée, les libellés suivent.
    page.fill("#week_start", "2026-12-01")
    expect(page.locator("#week_end")).to_have_value("2026-12-04")
    expect(page.locator(".b-rowlabel")).to_have_text(["Mar 01/12", "Mer 02/12", "Jeu 03/12", "Ven 04/12"])

    page.click("#btn-b")
    page.wait_for_url(f"{server}/planning/{pid}")  # même planning, pas de copie
    expect(page.locator(".hero-sub strong")).to_have_text("Mardi 01/12 → vendredi 04/12")
    expect(page.locator(".week-grid .day-head > span:first-child")).to_have_text(
        ["Mar 01/12", "Mer 02/12", "Jeu 03/12", "Ven 04/12"])
    # B5 : aucun jour hors période affiché « Absent » (grille et liste mobile).
    expect(page.locator(".off-lbl")).to_have_count(0)
    # Groupes de midis → cases fusionnées.
    expect(page.locator('.week-grid .meal.midi.merged[data-jours="1,2"] .meal-name')).to_have_text("Poulet rôti")
    expect(page.locator('.week-grid .meal.midi.merged[data-jours="3,4"] .meal-name')).to_have_text("Gratin dauphinois")

    page.click("#btn-valider")
    expect(page.locator(".validated-badge")).to_be_visible()
    page.goto(server + "/historique")
    expect(page.locator(f'#valid-grid [data-card="{pid}"] .date')).to_contain_text("Mardi 01/12 → vendredi 04/12")


def test_refaire_puis_reprendre(page: Page, server):
    """« Refaire » une semaine validée commencée un mercredi, puis « Reprendre »
    la copie : grille pré-remplie, jour 1 = aujourd'hui (B3), enregistrement
    sur le même brouillon (B2)."""
    cases = semaine(3, {"1:midi": ("p1", 1), "2:midi": (None, 1), "1:soir": "c1", "3:soir": "s1"})
    src = semaine_validee(page, server, cases, "2026-11-04")

    page.goto(server + "/historique")
    page.click(f'.hist-dup[data-id="{src}"]')
    page.wait_for_url(re.compile(r"/planning/\d+$"))
    copie = int(page.url.rsplit("/", 1)[1])
    assert copie != src

    page.goto(server + "/historique")
    page.locator(f'#drafts-grid [data-card="{copie}"]').get_by_role("link", name="✏️ Reprendre").click()
    page.wait_for_url(f"{server}/?draft={copie}")
    expect(page.locator("#draft-id-input")).to_have_value(str(copie))
    expect(page.locator("#week_start")).to_have_value(date.today().isoformat())
    expect(page.locator(".b-rowlabel")).to_have_count(3)
    expect(cellule(page, "1:midi").locator(".b-main")).to_contain_text("Poulet rôti")
    expect(cellule(page, "2:midi").locator(".b-main.ro")).to_contain_text("Poulet rôti")
    expect(cellule(page, "1:soir").locator(".b-main")).to_contain_text("Curry de lentilles")
    expect(cellule(page, "3:soir").locator(".b-main")).to_contain_text("Soupe de potiron")

    page.click("#btn-draft")
    page.wait_for_url(f"{server}/?draft={copie}&saved=1")


def test_restes_vers_le_midi_suivant(page: Page, server):
    """Menu « ⋯ » → « Restes » : le soir du jour 1 passe au midi du jour 2, qui
    sort du groupe du jour 1 (le midi du jour 1 garde son plat)."""
    debut = prochain(0)
    page.goto(server + "/")
    periode(page, debut.isoformat(), (debut + timedelta(days=2)).isoformat())
    expect(page.locator(".b-rowlabel")).to_have_count(3)
    choisir_plat(page, "1:midi", "Poulet rôti")
    choisir_plat(page, "1:soir", "Curry de lentilles", recherche="curry")
    expect(cellule(page, "2:midi").locator(".b-main.ro")).to_contain_text("Poulet rôti")

    cellule(page, "1:soir").locator(".b-more").click()
    page.get_by_role("menuitem", name=re.compile("Restes")).click()
    expect(cellule(page, "2:midi").locator(".b-main")).to_contain_text("Curry de lentilles")
    expect(cellule(page, "2:midi").locator(".b-main.ro")).to_have_count(0)
    expect(cellule(page, "1:midi").locator(".b-main")).to_contain_text("Poulet rôti")
    expect(cellule(page, "1:soir").locator(".b-main")).to_contain_text("Curry de lentilles")

    page.click("#btn-b")
    page.wait_for_url(re.compile(r"/planning/\d+$"))
    expect(page.locator('.week-grid .meal.midi[data-jours="1"] .meal-name')).to_have_text("Poulet rôti")
    expect(page.locator('.week-grid .meal.midi[data-jours="2"] .meal-name')).to_have_text("Curry de lentilles")


def _semaine_validee_ouverte(page: Page, server) -> int:
    cases = semaine(2, {"1:midi": "p1", "1:soir": "c1", "2:midi": "g1", "2:soir": "s1"})
    pid = semaine_validee(page, server, cases, "2026-10-14")
    page.goto(f"{server}/planning/{pid}")
    page.click("#btn-edit-builder")
    page.wait_for_url(f"{server}/?planning={pid}")
    expect(page.locator("#draft-id-input")).to_have_value(str(pid))
    expect(page.locator("#week_start")).to_have_value("2026-10-14")
    expect(cellule(page, "1:soir").locator(".b-main")).to_contain_text("Curry de lentilles")
    return pid


def test_reouverture_semaine_validee(page: Page, server):
    """« ✏️ Modifier dans le constructeur » sur une semaine validée : la
    modification est enregistrée sur la même semaine, qui reste validée."""
    pid = _semaine_validee_ouverte(page, server)
    cellule(page, "1:soir").locator('[data-act="clear-main"]').click()
    choisir_plat(page, "1:soir", "Bœuf bourguignon", recherche="bourguignon")
    page.click("#btn-b")
    page.wait_for_url(f"{server}/planning/{pid}")
    expect(page.locator(".validated-badge")).to_be_visible()
    expect(page.locator('.week-grid .meal.soir[data-jours="1"] .meal-name')).to_have_text("Bœuf bourguignon")


@pytest.mark.xfail(strict=True, reason="Défaut connu : « Brouillon enregistré ✓ » affiché pour une semaine "
                                       "validée rouverte (app/static/js/builder.js:743)")
def test_reouverture_validee_message_enregistrement(page: Page, server):
    """Enregistrer sans valider une semaine validée rouverte : elle reste
    validée, le message ne doit pas parler de brouillon."""
    pid = _semaine_validee_ouverte(page, server)
    page.click("#btn-draft")
    page.wait_for_url(f"{server}/?planning={pid}&saved=1")
    assert "Brouillon" not in _toast(page)


@pytest.mark.xfail(strict=True, reason="Défaut connu : dernier jour de période à 0 convive ignoré dans nb_jours "
                                       "(app/services/planning.py:662, app/static/js/builder.js:73)")
def test_periode_dernier_jour_absent(page: Page, server):
    """Période de 4 jours dont le dernier est entièrement absent : la période
    reste de 4 jours (jour 4 affiché « Absent »), dans le planning comme à la
    réouverture."""
    debut = prochain(2)  # un mercredi
    fin = debut + timedelta(days=3)
    page.goto(server + "/")
    periode(page, debut.isoformat(), fin.isoformat())
    choisir_plat(page, "1:soir", "Soupe de potiron")
    for key in ("4:midi", "4:soir"):
        champ = cellule(page, key).locator(".b-pers")
        champ.fill("0")
        champ.press("Tab")  # change natif au blur → re-rendu de la grille
    page.click("#btn-draft")
    page.wait_for_url(re.compile(r"/\?draft=\d+&saved=1$"))
    pid = int(re.search(r"draft=(\d+)", page.url).group(1))
    expect(page.locator("#week_end")).to_have_value(fin.isoformat(), timeout=1000)
    page.goto(f"{server}/planning/{pid}")
    expect(page.locator(".week-grid .day-head")).to_have_count(4, timeout=1000)


def test_construire_sans_repas_reste_sur_la_page(page: Page, server):
    """Garde-fou : valider une grille vide n'envoie rien (message d'erreur)."""
    page.goto(server + "/")
    page.click("#btn-b")
    expect(page.locator("#toast-zone .toast-error")).to_contain_text("au moins un repas")
    assert page.url == server + "/"
