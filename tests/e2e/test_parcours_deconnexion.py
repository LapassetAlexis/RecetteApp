"""Parcours avec authentification (M09) : connexion, bouton « Déconnexion »
du menu (formulaire POST) en 1280 et 390 px, limite des échecs de connexion.

Serveur dédié avec authentification : fixture `server_auth` (tests/e2e/conftest.py)."""

import pytest
from playwright.sync_api import Browser, expect

from tests.e2e.outils import connexion as _connexion
from tests.e2e.outils import contexte as _contexte
from tests.e2e.outils import pas_de_defilement_horizontal

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("largeur", [1280, 390])
def test_deconnexion_par_le_menu(browser: Browser, server_auth, largeur):
    ctx, page, erreurs = _contexte(browser, largeur)
    try:
        _connexion(page, server_auth)
        expect(page).to_have_url(server_auth + "/historique")
        bouton = page.locator("form.nav-logout button")
        expect(bouton).to_be_visible()
        expect(bouton).to_have_text("Déconnexion")
        assert page.locator('a[href="/logout"]').count() == 0
        pas_de_defilement_horizontal(page)
        # Même présentation que les liens du menu (pas de bouton natif gris).
        styles = page.evaluate("""() => {
            const b = getComputedStyle(document.querySelector('.nav-logout button'));
            const a = getComputedStyle(document.querySelector('.nav-links a'));
            return {bg: b.backgroundColor, bord: b.borderTopWidth, taille: [b.fontSize, a.fontSize],
                    haut: document.querySelector('.nav-logout button').getBoundingClientRect().height};
        }""")
        assert styles["bg"] in ("rgba(0, 0, 0, 0)", "transparent") and styles["bord"] == "0px"
        assert styles["taille"][0] == styles["taille"][1]
        if largeur < 500:
            assert styles["haut"] >= 44

        bouton.click()
        expect(page).to_have_url(server_auth + "/login")
        page.goto(server_auth + "/historique")
        expect(page).to_have_url(server_auth + "/login?next=%2Fhistorique")   # session fermée
        assert not erreurs, erreurs
    finally:
        ctx.close()


def test_trop_d_echecs_de_connexion(browser: Browser, server_auth):
    ctx, page, erreurs = _contexte(browser, 1280)
    try:
        for _ in range(5):
            _connexion(page, server_auth, "mauvais")
            expect(page.locator("body")).to_contain_text("Identifiants incorrects")
        _connexion(page, server_auth, "secret")
        expect(page.locator("body")).to_contain_text("Trop de tentatives")
        assert not erreurs, erreurs
    finally:
        ctx.close()
