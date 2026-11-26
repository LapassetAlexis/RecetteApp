"""Mode cuisine (M11) : minuteur lancé depuis une étape, décompte et fin
pilotés par une horloge simulée (`page.clock`), et fonctions pures de
`window.Cuisine` exécutées dans le navigateur."""

import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_minuteur_decompte_et_sonne_a_la_fin(page: Page, server):
    """En 390 px : onglets Ingrédients / Étapes (en 1280, les deux sont côte à côte)."""
    page.set_viewport_size({"width": 390, "height": 844})
    page.clock.install()
    page.goto(f"{server}/recette/c1/cuisine")
    page.click("#tab-steps")
    page.click("#ck-next")
    etape = page.locator("#ck-steps li").nth(1)
    bouton = etape.locator(".ck-dur")
    expect(bouton).to_have_text("⏱ 20 min")
    bouton.click()

    minuteur = page.locator(".ck-timer")
    expect(minuteur).to_have_count(1)
    expect(minuteur.locator(".ck-timer-l")).to_have_text("Étape 2 · 20 min")
    expect(minuteur.locator(".ck-timer-c")).to_have_text("20:00")

    page.clock.run_for("01:00")
    expect(minuteur.locator(".ck-timer-c")).to_have_text("19:00")
    page.clock.run_for("18:30")
    expect(minuteur.locator(".ck-timer-c")).to_have_text("00:30")
    expect(minuteur).not_to_have_class(re.compile(r"\bdone\b"))

    page.clock.run_for("00:31")
    expect(minuteur.locator(".ck-timer-c")).to_have_text("Terminé !")
    expect(minuteur).to_have_class(re.compile(r"\bdone\b"))
    expect(minuteur.locator(".ck-timer-x")).to_have_text("OK")

    # Rechargement : le minuteur terminé est restauré (sessionStorage), puis « OK »
    # le retire. Mouvement réduit : la pastille ne pulse plus (bouton stable).
    page.emulate_media(reduced_motion="reduce")
    page.reload()
    expect(page.locator(".ck-timer .ck-timer-c")).to_have_text("Terminé !")
    page.locator(".ck-timer-x").click()
    expect(page.locator(".ck-timer")).to_have_count(0)
    expect(page.locator("#ck-timers")).to_be_hidden()


def test_deux_minuteurs_en_parallele(page: Page, server):
    page.clock.install()
    page.goto(f"{server}/recette/c1/cuisine")
    expect(page.locator("#tab-steps")).to_be_hidden()   # 1280 : pas d'onglets
    page.locator("#ck-steps li").first.locator(".ck-dur").click()
    page.click("#ck-next")
    page.locator("#ck-steps li").nth(1).locator(".ck-dur").click()
    comptes = page.locator(".ck-timer .ck-timer-c")
    expect(comptes).to_have_text(["01:00", "20:00"])
    page.clock.run_for("01:05")
    expect(comptes).to_have_text(["Terminé !", "18:55"])


def test_fonctions_pures_de_window_cuisine(page: Page, server):
    page.goto(f"{server}/recette/c1/cuisine")
    res = page.evaluate("""() => {
        const C = window.Cuisine;
        const dur = t => C.parseDurations(t).map(d => d.seconds);
        return {
            simple: dur('Cuire 20 min puis 45 minutes'),
            heures: dur('1 h 30, 1h30, 2 heures, 1,5 h'),
            fourchette: dur('15 à 20 min'),
            pieges: dur('2 huiles, 2 minces, 3.5'),
            secondes: dur('30 secondes'),
            formats: [C.formatDuration(1200), C.formatDuration(5400), C.formatDuration(3600),
                      C.formatDuration(90), C.formatDuration(5)],
            horloges: [C.formatClock(1200), C.formatClock(5400), C.formatClock(0.2), C.formatClock(-3)],
            html: C.stepHtml('Four <b> 10 min', s => String(s).replace(/</g, '&lt;')),
        };
    }""")
    assert res["simple"] == [1200, 2700]
    assert res["heures"] == [5400, 5400, 7200, 5400]
    assert res["fourchette"] == [1200]
    assert res["pieges"] == []
    assert res["secondes"] == [30]
    assert res["formats"] == ["20 min", "1 h 30", "1 h", "1 min 30", "5 s"]
    assert res["horloges"] == ["20:00", "1:30:00", "00:01", "00:00"]
    assert "&lt;b>" in res["html"] and 'class="ck-dur" data-s="600"' in res["html"]
