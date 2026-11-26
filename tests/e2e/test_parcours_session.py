"""Instance avec authentification (M11) : connexion, puis session expirée
pendant une coche de la liste de courses → message « Session expirée » et
retour au formulaire de connexion, qui ramène sur le planning.

Serveur dédié avec authentification : fixture `server_auth`."""

import re
from urllib.parse import quote

import pytest
from playwright.sync_api import Browser, expect

from tests.e2e.outils import connexion, contexte, semaine, semaine_validee

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("largeur", [1280, 390])
def test_session_expiree_pendant_une_coche(browser: Browser, server_auth, largeur):
    server = server_auth
    ctx, page, erreurs = contexte(browser, largeur)
    try:
        connexion(page, server)
        expect(page).to_have_url(server + "/historique")
        # Requêtes du contexte : elles portent le cookie de session.
        pid = semaine_validee(page, server, semaine(1, {"1:midi": "g1"}), "2026-11-02")
        assert page.request.post(f"{server}/generate-shopping/{pid}").json()["success"]
        page.goto(f"{server}/planning/{pid}")
        ligne = page.locator("#shop-g .shop-i").first
        expect(ligne).to_be_visible()

        # La session expire (cookie perdu) : la coche reçoit un 401.
        ctx.clear_cookies()
        with page.expect_response(lambda r: "/api/shopping-check/" in r.url) as rep:
            ligne.locator(".shop-c").click()
        assert rep.value.status == 401
        expect(page.get_by_text("Session expirée, reconnecte-toi")).to_be_visible()
        cible = f"/planning/{pid}"
        expect(page).to_have_url(re.compile(re.escape(f"{server}/login?next={quote(cible, safe='')}") + "$"))

        # Reconnexion : retour au planning.
        page.fill('input[name="username"]', "chef")
        page.fill('input[name="password"]', "secret")
        page.locator('button[type="submit"]').click()
        expect(page).to_have_url(server + cible)
        assert not erreurs, erreurs
    finally:
        ctx.close()
