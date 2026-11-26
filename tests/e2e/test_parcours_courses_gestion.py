"""M06 — gestion des courses au navigateur : placard, rayon d'un article
manuel, lien révocable, rafraîchissement de la page planning, confirmation
de la régénération.

- un article mis au placard depuis un planning se retire depuis l'écran du
  placard (modale), sans ouvrir ce planning ;
- une coche faite sur le lien public apparaît sur la page planning ouverte,
  sans rechargement, au prochain rafraîchissement (20 s, horloge avancée) ;
- un lien révoqué : la page publique ouverte affiche « Ce lien n'est plus
  valide » et vide sa file hors-ligne."""

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.outils import semaine, semaine_validee

pytestmark = pytest.mark.e2e

CACHE = "menu-planner-v6"


def _planning(page: Page, server: str, week_start: str) -> int:
    """Planning validé (gratin + quiche), liste générée, page planning ouverte."""
    pid = semaine_validee(page, server, semaine(2, {"1:midi": "g1", "2:soir": "q1"}), week_start)
    assert "error" not in page.request.post(f"{server}/generate-shopping/{pid}").json()
    page.goto(f"{server}/planning/{pid}")
    expect(page.locator("#shop-g")).to_be_visible()
    return pid


def _ligne(page: Page, texte: str):
    return page.locator("#shop-g .shop-i").filter(has_text=texte)


def _menu(page: Page, ligne) -> None:
    ligne.locator(".shop-more").click()
    expect(ligne.locator(".shop-acts")).to_be_visible()


def _sans_debordement(page: Page) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("largeur", [1280, 390])
def test_placard_retire_depuis_son_ecran(page: Page, server, largeur, request):
    page.set_viewport_size({"width": largeur, "height": 900})
    # Le placard vaut pour toute la base e2e : on le laisse vide en sortant.
    request.addfinalizer(lambda: page.request.delete(f"{server}/api/placard/lardon"))
    _planning(page, server, "2027-01-04" if largeur == 1280 else "2027-01-11")
    lardons = _ligne(page, "Lardons")
    _menu(page, lardons)
    with page.expect_response(lambda r: r.url.endswith("/placard")):
        lardons.locator('[data-act="placard"]').click()
    expect(_ligne(page, "Lardons")).to_have_count(0)
    expect(page.locator("#placard-n")).to_have_text("1")

    # Écran du placard : liste complète, piège à focus, retrait.
    page.click("#btn-placard")
    modale = page.locator("#placard-modal")
    expect(modale).to_be_visible()
    # Le placard garde le nom normalisé (singulier, minuscules).
    item = modale.locator("li").filter(has_text="Lardon")
    expect(item).to_have_count(1)
    _sans_debordement(page)
    for _ in range(6):
        page.keyboard.press("Tab")
        assert page.evaluate("document.getElementById('placard-modal').contains(document.activeElement)")
    with page.expect_response(lambda r: "/api/placard/" in r.url and r.request.method == "DELETE"):
        item.locator("button").click()
    expect(modale.locator("li")).to_have_count(0)
    expect(modale.locator("#placard-vide")).to_be_visible()
    page.keyboard.press("Escape")
    expect(modale).to_be_hidden()
    # L'article est revenu dans la liste, compteur à jour, sans rechargement.
    expect(_ligne(page, "Lardons")).to_have_count(1)
    expect(page.locator("#placard-n")).to_have_text("0")


def test_rayon_d_un_article_manuel(page: Page, server, erreurs_console):
    _planning(page, server, "2027-01-18")
    page.fill("#shop-add-t", "Papier toilette (x6)")
    with page.expect_response(lambda r: r.url.endswith("/extra")):
        page.press("#shop-add-t", "Enter")
    article = page.locator("#shop-g .shop-i.shop-extra").filter(has_text="Papier toilette (x6)")
    expect(article).to_have_count(1)
    _menu(page, article)
    with page.expect_response(lambda r: r.url.endswith("/rayon")):
        article.locator(".shop-rayon-sel").select_option("Surgelés")
    rayon = page.locator('#shop-g .shop-rayon[data-rayon="Surgelés"]')
    expect(rayon.locator(".shop-i").filter(has_text="Papier toilette (x6)")).to_have_count(1)

    # Un nom sans lettre est refusé.
    page.fill("#shop-add-t", "2")
    with page.expect_response(lambda r: r.url.endswith("/extra")):
        page.press("#shop-add-t", "Enter")
    expect(page.locator(".toast-error")).to_contain_text("Article refusé")
    # Le navigateur journalise le 400 attendu.
    erreurs_console[:] = [e for e in erreurs_console if "status of 400" not in e]


def test_coche_publique_puis_revocation(page: Page, browser: Browser, server):
    page.clock.install()
    pid = _planning(page, server, "2027-01-25")
    with page.expect_response(lambda r: r.url.endswith(f"/planning/{pid}/partager")) as rep:
        page.click("#btn-share")
    lien = rep.value.json()["url"]
    expect(page.locator("#btn-unshare")).to_be_visible()

    ctx = browser.new_context(viewport={"width": 390, "height": 844}, service_workers="block")
    erreurs: list[str] = []
    pub = ctx.new_page()
    pub.on("pageerror", lambda e: erreurs.append(str(e)))
    try:
        pub.goto(server + lien)
        cle = pub.locator("#shop-g .pub-i").first.get_attribute("data-item")
        with pub.expect_response(lambda r: r.url.endswith("/check")):
            pub.locator(f'.pub-i[data-item="{cle}"] .shop-c').check()

        # Page planning : la coche arrive au rafraîchissement suivant (20 s).
        case = page.locator(f'#shop-g .shop-i[data-item="{cle}"] .shop-c')
        expect(case).not_to_be_checked()
        with page.expect_response(lambda r: r.url.endswith(f"/api/courses/{pid}/etat")):
            page.clock.run_for(21000)
        expect(case).to_be_checked()

        # File hors-ligne factice sur la page publique, puis révocation.
        pub.evaluate("(k) => localStorage.setItem(k, JSON.stringify({'x|': true}))",
                     f"courses-file:{lien}")
        page.once("dialog", lambda d: d.accept())
        with page.expect_response(lambda r: r.url.endswith("/partager")) as rev:
            page.click("#btn-unshare")
        assert rev.value.json() == {"url": None}
        expect(page.locator("#btn-unshare")).to_be_hidden()

        # Page publique ouverte : au prochain sondage, message et file vidée.
        with pub.expect_response(lambda r: r.url.endswith("/etat")):
            pub.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        expect(pub.locator("#pub-revoque")).to_be_visible()
        expect(pub.locator("#pub-revoque")).to_contain_text("Ce lien n'est plus valide")
        expect(pub.locator("#pub-liste")).to_be_hidden()
        assert pub.evaluate("(k) => localStorage.getItem(k)", f"courses-file:{lien}") is None

        # Rechargement : la page 404 dit la même chose.
        pub.reload()
        expect(pub.locator("#pub-revoque")).to_be_visible()

        # « Partager » recrée un lien, différent.
        with page.expect_response(lambda r: r.url.endswith("/partager")) as nouveau:
            page.click("#btn-share")
        assert nouveau.value.json()["url"] not in (None, lien)
    finally:
        ctx.close()
    assert not erreurs, erreurs


def test_regenerer_demande_confirmation_si_coche(page: Page, server):
    pid = _planning(page, server, "2027-02-01")
    with page.expect_response(lambda r: "/api/shopping-check/" in r.url):
        _ligne(page, "Lardons").locator(".shop-c").check()

    messages: list[str] = []

    def _refuser(d):
        messages.append(d.message)
        d.dismiss()
    page.once("dialog", _refuser)
    page.click("#btn-regen")
    expect(_ligne(page, "Lardons").locator(".shop-c")).to_be_checked()
    assert messages and "décochés" in messages[0]
    etat = page.request.get(f"{server}/api/courses/{pid}/etat").json()
    assert etat["checked"]

    page.once("dialog", lambda d: d.accept())
    with page.expect_navigation():
        page.click("#btn-regen")
    expect(_ligne(page, "Lardons").locator(".shop-c")).not_to_be_checked()


def test_service_worker_oublie_le_lien_revoque(page: Page, browser: Browser, server):
    """Vrai service worker : la liste publique est en cache ; une fois le lien
    révoqué, le 404 de l'état la retire du cache (plus d'ouverture hors ligne)."""
    pid = semaine_validee(page, server, semaine(2, {"1:midi": "g1"}), "2027-02-08")
    assert "error" not in page.request.post(f"{server}/generate-shopping/{pid}").json()
    lien = page.request.post(f"{server}/planning/{pid}/partager").json()["url"]
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, locale="fr-FR",
                              service_workers="allow")
    pub = ctx.new_page()
    en_cache = """async ([nom, chemin]) => {
        const c = await caches.open(nom);
        return [!!(await c.match(chemin)), !!(await c.match(chemin + '/etat'))];
    }"""
    try:
        pub.goto(server + lien)
        assert pub.evaluate("navigator.serviceWorker.ready.then(r => !!r.active)")
        pub.reload()   # page contrôlée : ses requêtes passent par le service worker
        expect(pub.locator("#shop-g .pub-i").first).to_be_visible()
        pub.wait_for_function(f"({en_cache})(['{CACHE}', '{lien}']).then(v => v[0] && v[1])")

        assert page.request.post(f"{server}/planning/{pid}/partager",
                                 data={"action": "revoquer"}).json() == {"url": None}
        pub.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        expect(pub.locator("#pub-revoque")).to_be_visible()
        pub.wait_for_function(f"({en_cache})(['{CACHE}', '{lien}']).then(v => !v[0] && !v[1])")
    finally:
        ctx.close()
