"""Parcours avec authentification (M09) : connexion, bouton « Déconnexion »
du menu (formulaire POST) en 1280 et 390 px, limite des échecs de connexion.

Serveur dédié : l'application est importée AVANT `fake_notion.serve` avec
AUTH_USER / AUTH_PASSWORD définis (le middleware d'auth est posé à l'import)."""

import os
import subprocess
import sys
import time
import urllib.request

import pytest
from playwright.sync_api import Browser, expect

from tests.e2e.conftest import RACINE, _port_libre
from tests.e2e.outils import pas_de_defilement_horizontal

pytestmark = pytest.mark.e2e

_LANCEUR = ("import sys, app.main; from tests.e2e.fake_notion import serve; "
            "serve(sys.argv[1], int(sys.argv[2]))")


# Un serveur par test : la limite des échecs (en mémoire) ne déborde pas d'un test à l'autre.
@pytest.fixture
def server_auth(tmp_path_factory):
    port = _port_libre()
    db = tmp_path_factory.mktemp("e2e-auth") / "auth.db"
    log = open(db.with_suffix(".log"), "w")  # noqa: SIM115 — fermé en fin de test
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "AUTH_USER": "chef", "AUTH_PASSWORD": "secret",
           "DATABASE_PATH": str(db)}
    proc = subprocess.Popen([sys.executable, "-c", _LANCEUR, str(db), str(port)],
                            cwd=RACINE, stdout=log, stderr=subprocess.STDOUT, env=env)
    url = f"http://127.0.0.1:{port}"
    fin = time.monotonic() + 30
    while True:
        try:
            with urllib.request.urlopen(url + "/health", timeout=1) as r:
                if r.status == 200:
                    break
        except OSError:
            pass
        if proc.poll() is not None or time.monotonic() > fin:
            proc.kill()
            log.close()
            raise RuntimeError("Serveur e2e (auth) non démarré :\n" + db.with_suffix(".log").read_text())
        time.sleep(0.1)
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


def _contexte(browser: Browser, largeur: int):
    mobile = largeur < 500
    ctx = browser.new_context(viewport={"width": largeur, "height": 844 if mobile else 900},
                              is_mobile=mobile, has_touch=mobile, locale="fr-FR",
                              service_workers="block")
    page = ctx.new_page()
    erreurs: list[str] = []
    page.on("pageerror", lambda e: erreurs.append(f"pageerror: {e}"))
    # Les refus attendus (401 / 429 du formulaire) sont des réponses, pas des erreurs JS.
    page.on("console", lambda m: erreurs.append(f"console: {m.text}")
            if m.type == "error" and "Failed to load resource" not in m.text else None)
    return ctx, page, erreurs


def _connexion(page, server, mot_de_passe="secret"):
    page.goto(server + "/historique")
    expect(page).to_have_url(server + "/login?next=%2Fhistorique")
    page.fill('input[name="username"]', "chef")
    page.fill('input[name="password"]', mot_de_passe)
    page.locator('button[type="submit"]').click()


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
