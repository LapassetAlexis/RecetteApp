"""Socle front (M01) : formulaires traités en JS (N1), session expirée et
réponse non JSON (N6), spinner conservé sur les formulaires qui naviguent."""

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.outils import construire, semaine, semaine_validee

pytestmark = pytest.mark.e2e

ACTIF = re.compile(r"\bactive\b")
CHARGE = re.compile(r"\bloading\b")


def _page_planning(page: Page, server, week_start: str) -> int:
    pid = semaine_validee(page, server, semaine(2, {"1:midi": "p1", "2:soir": "c1"}), week_start)
    page.goto(f"{server}/planning/{pid}")
    if not page.locator("#shop-add").is_visible():
        with page.expect_navigation():
            page.click("#btn-regen")
    return pid


def _ajouter(page: Page, texte: str, au_clavier: bool) -> None:
    page.fill("#shop-add-t", texte)
    with page.expect_response(lambda r: r.url.endswith("/extra") and r.request.method == "POST"):
        if au_clavier:
            page.press("#shop-add-t", "Enter")
        else:
            page.click("#shop-add button[type=submit]")


def _articles(page: Page):
    return page.locator("#shop-body .shop-i.shop-extra")


@pytest.mark.parametrize("au_clavier", [True, False], ids=["clavier", "clic"])
def test_deux_articles_a_la_suite(page: Page, server, au_clavier):
    _page_planning(page, server, "2026-10-05" if au_clavier else "2026-10-12")
    bouton = page.locator("#shop-add button[type=submit]")

    _ajouter(page, "café", au_clavier)
    expect(_articles(page)).to_have_count(1)
    expect(bouton).to_be_enabled()
    expect(bouton).not_to_have_class(CHARGE)
    expect(page.locator("#nav-progress")).not_to_have_class(ACTIF)

    _ajouter(page, "2 l lait", au_clavier)
    expect(_articles(page)).to_have_count(2)
    expect(bouton).to_be_enabled()
    expect(bouton).not_to_have_class(CHARGE)
    expect(page.locator("#nav-progress")).not_to_have_class(ACTIF)


def test_fiche_ajout_au_planning_sans_barre_bloquee(page: Page, server):
    page.goto(f"{server}/recette/s1")
    page.click("#btn-planifier")
    page.select_option("#plan-draft", "new")
    page.fill("#plan-date", "2026-10-20")
    with page.expect_response(lambda r: r.url.endswith("/api/recette/s1/planifier")):
        page.click("#plan-ok")
    expect(page.locator("#plan-done")).to_be_visible()
    expect(page.locator("#nav-progress")).not_to_have_class(ACTIF)
    expect(page.locator("#plan-ok")).not_to_have_class(CHARGE)
    expect(page.locator("#plan-ok")).to_be_enabled()


_OBSERVER = """() => {
    // Écouteur sur window : passe après celui de base.html (document). Il relève
    // l'état du bouton puis annule l'envoi pour pouvoir l'observer.
    window.__etat = null;
    window.addEventListener('submit', (e) => {
        e.preventDefault();
        const b = e.submitter;
        window.__etat = {
            bouton: b ? b.id || b.textContent.trim() : null,
            loading: !!b && b.classList.contains('loading'),
            disabled: !!b && b.disabled,
            nav: document.getElementById('nav-progress').classList.contains('active'),
        };
    });
}"""


def _spinner_conserve(page: Page, bouton: str) -> None:
    page.evaluate(_OBSERVER)
    page.click(bouton)
    page.wait_for_function("window.__etat !== null")
    etat = page.evaluate("window.__etat")
    assert etat["loading"] and etat["disabled"] and etat["nav"], etat


def test_formulaires_qui_naviguent_gardent_leur_spinner(page: Page, server):
    # Constructeur : brouillon rouvert puis « Enregistrer le brouillon ».
    pid = construire(page, server, semaine(2, {"1:midi": "g1"}), "2026-10-26", action="brouillon")
    page.goto(f"{server}/?draft={pid}")
    _spinner_conserve(page, "#btn-draft")

    # Ajout de recette (saisie manuelle).
    page.goto(f"{server}/ajouter")
    page.click("#btn-manual")
    page.fill("#nom", "Recette de test e2e")
    _spinner_conserve(page, "#btn-add")

    # Import de sauvegarde.
    page.goto(f"{server}/admin")
    page.set_input_files("#fichier", files=[{"name": "s.json", "mimeType": "application/json", "buffer": b"{}"}])
    _spinner_conserve(page, "form[action='/admin/import'] button[type=submit]")


def _sans_erreur_de_chargement(erreurs: list[str], statut: int) -> None:
    """Le navigateur journalise une ressource en erreur HTTP : attendu ici."""
    erreurs[:] = [e for e in erreurs if f"status of {statut}" not in e]


def test_session_expiree_redirige_vers_login(page: Page, server, erreurs_console):
    pid = _page_planning(page, server, "2026-11-09")
    page.route("**/api/shopping-check/**", lambda r: r.fulfill(status=401, body="Authentification requise"))
    with page.expect_request(lambda r: "/login?next=" in r.url and r.resource_type == "document") as req:
        page.locator("#shop-g .shop-c").first.check()
        expect(page.locator(".toast-error")).to_have_text("Session expirée, reconnecte-toi")
    assert req.value.url.endswith(f"/login?next=%2Fplanning%2F{pid}")
    _sans_erreur_de_chargement(erreurs_console, 401)


def test_session_expiree_sur_une_action_de_page(page: Page, server):
    """Action hors /api/ : le 303 vers /login est suivi par fetch, la réponse
    finale est la page de connexion → même traitement. Sans auth, le serveur
    e2e renverrait aussitôt ailleurs : on simule donc la réponse finale."""
    pid = _page_planning(page, server, "2026-11-16")
    page.evaluate("""() => {
        window.fetch = async () => ({
            status: 200, ok: true, redirected: true, url: location.origin + '/login?next=%2Fx',
            json: async () => { throw new SyntaxError('HTML'); },
        });
    }""")
    with page.expect_request(lambda r: "/login?next=" in r.url and r.resource_type == "document") as req:
        page.click("#btn-share")
        expect(page.locator(".toast-error")).to_have_text("Session expirée, reconnecte-toi")
    assert req.value.url.endswith(f"/login?next=%2Fplanning%2F{pid}")


def test_reponse_non_json(page: Page, server, erreurs_console):
    _page_planning(page, server, "2026-11-23")
    page.route("**/partager", lambda r: r.fulfill(status=502, content_type="text/html", body="<h1>Bad gateway</h1>"))
    page.click("#btn-share")
    expect(page.locator(".toast-error")).to_have_text("Réponse inattendue du serveur")
    expect(page.locator("#btn-share")).to_be_enabled()
    _sans_erreur_de_chargement(erreurs_console, 502)


def test_modale_commune_piege_le_focus(page: Page, server):
    """`Common.modal` : focus initial, Tab / Maj+Tab piégés, Échap ferme et
    rend le focus au déclencheur, `onClose` appelé une seule fois."""
    page.goto(f"{server}/recette/p1")  # page qui charge common.js
    page.evaluate("""() => {
        const d = document.createElement('div');
        d.innerHTML = `<button id="m-open">Ouvrir</button>
            <div id="m" class="modal" style="display:none"><div class="modal-box">
            <button id="m-a">A</button><input id="m-b"><button id="m-c">C</button></div></div>`;
        document.body.appendChild(d);
        window.__ferme = 0;
        document.getElementById('m-open').addEventListener('click', () => {
            window.__m = Common.modal(document.getElementById('m'),
                {initialFocus: '#m-b', onClose: () => { window.__ferme += 1; }});
        });
    }""")
    page.click("#m-open")
    expect(page.locator("#m")).to_be_visible()
    expect(page.locator("#m-b")).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.locator("#m-c")).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.locator("#m-a")).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(page.locator("#m-c")).to_be_focused()
    page.keyboard.press("Escape")
    expect(page.locator("#m")).to_be_hidden()
    expect(page.locator("#m-open")).to_be_focused()
    assert page.evaluate("window.__ferme") == 1
    page.evaluate("window.__m.close()")  # seconde fermeture : sans effet
    assert page.evaluate("window.__ferme") == 1
