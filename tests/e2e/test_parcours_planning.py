"""Parcours de la page planning : case fusionnée (groupe de midis), modale
« Changer » avec un nom de recette piégé (B17), accompagnement libre, et
édition groupée en une requête (repas libre, brique : une seule recette
créée pour les deux jours)."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fake_notion import NOM_PIEGE
from tests.e2e.outils import semaine, semaine_validee

pytestmark = pytest.mark.e2e

FUSION = '.week-grid .meal.midi.merged[data-jours="1,2"]'


def _planning_fusionne(page: Page, server, week_start="2026-11-18") -> int:
    """Semaine validée de 3 jours : midis des jours 1 et 2 groupés (Poulet rôti)."""
    cases = semaine(3, {"1:midi": ("p1", 1), "2:midi": (None, 1), "1:soir": "c1", "3:soir": "s1"})
    pid = semaine_validee(page, server, cases, week_start)
    page.goto(f"{server}/planning/{pid}")
    expect(page.locator(f"{FUSION} .meal-name")).to_have_text("Poulet rôti")
    return pid


def test_changer_case_fusionnee_nom_piege_puis_accompagnement_libre(page: Page, server):
    """La recette `Salade "César" <b>x</b>` s'affiche intacte dans la modale,
    sans HTML injecté, et remplace le plat des deux jours fusionnés en tant que
    vraie recette (pas « repas libre ») — B17. Puis un accompagnement libre est
    ajouté aux deux jours en une fois."""
    _planning_fusionne(page, server)
    page.locator(f"{FUSION} .meal-ch").click()
    expect(page.locator("#modal")).to_be_visible()
    expect(page.locator("#modal-ctx")).to_have_text("Mer–Jeu midi")
    ligne = page.locator("#alt-list .alt").filter(has_text="César")
    expect(ligne.locator("span").first).to_have_text(NOM_PIEGE)
    expect(page.locator("#alt-list b")).to_have_count(0)  # aucun HTML interprété

    page.fill("#flt", "césar")
    expect(page.locator("#alt-list .alt:visible")).to_have_count(1)
    with page.expect_request(lambda r: r.url.endswith("/meals")) as req:
        page.get_by_role("button", name=f"Choisir {NOM_PIEGE}").click()
    corps = req.value.post_data_json
    assert corps["nom"] == NOM_PIEGE
    assert sorted((s["jour"], s["moment"]) for s in corps["slots"]) == [(1, "midi"), (2, "midi")]

    page.wait_for_load_state("load")
    expect(page.locator(f"{FUSION} .meal-name")).to_have_text(NOM_PIEGE)
    expect(page.locator(f"{FUSION} .meal-warn")).to_have_count(0)        # pas « 📝 Repas libre »
    expect(page.locator(f"{FUSION} a.meal-name")).to_have_attribute("href", "/recette/x1")

    # Accompagnement libre sur la case fusionnée : une requête, deux jours.
    page.locator(f"{FUSION} .side-add").click()
    expect(page.locator("#modal h3")).to_have_text("🥦 Ajouter un accompagnement")
    page.click("#free-toggle")
    page.fill("#free-nom", "Salade verte")
    with page.expect_request(lambda r: r.url.endswith("/meals")) as req:
        page.click("#free-go")
    assert req.value.post_data_json["action"] == "add_side"
    page.wait_for_load_state("load")
    side = page.locator(f"{FUSION} .side-item").filter(has_text="Salade verte")
    expect(side).to_have_count(1)
    expect(side.locator(".side-warn")).to_have_text("📝")


def _compter(page: Page, fragment: str) -> list:
    vus = []
    page.on("request", lambda r: vus.append(r.url) if fragment in r.url and r.method == "POST" else None)
    return vus


def test_repas_libre_case_fusionnee_une_requete(page: Page, server):
    _planning_fusionne(page, server, "2026-11-11")
    vus = _compter(page, "/api/free-meal/")
    page.locator(f"{FUSION} .meal-ch").click()
    page.click("#free-toggle")
    page.fill("#free-nom", "Steak frites")
    page.fill("#free-ings", "2 steaks\n500 g de pommes de terre")
    with page.expect_navigation():
        page.click("#free-go")
    expect(page.locator(f"{FUSION} .meal-name")).to_have_text("Steak frites")
    assert len(vus) == 1, f"{len(vus)} requêtes /api/free-meal pour une case fusionnée"


def test_brique_case_fusionnee_une_requete(page: Page, server):
    _planning_fusionne(page, server, "2026-11-12")
    vus = _compter(page, "/api/brique/")
    page.locator(f"{FUSION} .meal-ch").click()
    page.click("#brique-toggle")
    page.fill("#brique-nom", "Steak haché")
    page.locator("#brique-form label.tgl").filter(has_text="Viande").click()
    expect(page.locator(".brique-base[value='Viande']")).to_be_checked()
    with page.expect_navigation():
        page.click("#brique-go")
    expect(page.locator(f"{FUSION} .meal-name")).to_have_text("Steak haché")
    assert len(vus) == 1, f"{len(vus)} requêtes /api/brique pour une case fusionnée"
