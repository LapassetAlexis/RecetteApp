"""Parcours hors ligne avec un vrai service worker (M04, N9).

Les autres e2e bloquent le service worker (`conftest.py`) ; ici chaque test
crée son propre contexte avec `service_workers="allow"`. 127.0.0.1 est un
contexte sécurisé : le service worker s'enregistre sans HTTPS.

- liste publique ouverte, réseau coupé, rechargement, deux coches, retour du
  réseau → les deux coches sont enregistrées sur le serveur ;
- deux versions `?v=` d'un même fichier → une seule entrée en cache ;
- une page privée (/planning/<id>) n'est pas gardée : hors ligne, page de
  secours."""

import time

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, expect

from tests.e2e.outils import semaine, semaine_validee

pytestmark = pytest.mark.e2e

CACHE = "menu-planner-v6"


def _liste_partagee(page: Page, server: str, week_start: str) -> tuple[int, str]:
    """Planning validé, liste de courses générée et lien public."""
    pid = semaine_validee(page, server, semaine(2, {"1:midi": "g1", "1:soir": "q1", "2:soir": "b1"}), week_start)
    gen = page.request.post(f"{server}/generate-shopping/{pid}").json()
    assert "error" not in gen, gen
    lien = page.request.post(f"{server}/planning/{pid}/partager").json()["url"]
    return pid, lien


def _contexte_sw(browser: Browser) -> BrowserContext:
    return browser.new_context(viewport={"width": 390, "height": 844}, locale="fr-FR",
                               service_workers="allow")


def _attendre(page: Page, script: str, arg=None, timeout=10.0):
    """Évalue `script` (fonction async) jusqu'à un résultat vrai."""
    fin = time.monotonic() + timeout
    while True:
        res = page.evaluate(script, arg)
        if res or time.monotonic() > fin:
            return res
        time.sleep(0.1)


def _cles_du_cache(page: Page) -> list[str]:
    return page.evaluate("""async (nom) => {
        const c = await caches.open(nom);
        return (await c.keys()).map(r => r.url);
    }""", CACHE)


def _sw_actif(page: Page) -> None:
    assert page.evaluate("navigator.serviceWorker.ready.then(r => !!r.active)")


def test_liste_publique_hors_ligne_puis_synchro(page: Page, browser: Browser, server):
    _, lien = _liste_partagee(page, server, "2026-12-14")
    ctx = _contexte_sw(browser)
    erreurs: list[str] = []
    pub = ctx.new_page()
    pub.on("pageerror", lambda e: erreurs.append(str(e)))
    try:
        pub.goto(server + lien)
        _sw_actif(pub)
        # La page publique demande sa propre mise en cache (message 'cache-urls').
        assert _attendre(pub, """async ([nom, chemin]) => {
            const c = await caches.open(nom);
            return !!(await c.match(chemin)) && !!(await c.match(chemin + '/etat'));
        }""", [CACHE, lien]), _cles_du_cache(pub)

        lignes = pub.locator("#shop-g .pub-i")
        expect(lignes.first).to_be_visible()
        cles = lignes.evaluate_all("els => els.slice(0, 2).map(e => e.dataset.item)")
        assert len(cles) == 2

        # Réseau coupé, rechargement : la liste vient du cache.
        ctx.set_offline(True)
        pub.reload()
        expect(pub.locator("#pub-offline")).to_be_visible()
        for cle in cles:
            pub.locator(f'.pub-i[data-item="{cle}"] .shop-c').check()
        expect(pub.locator("#pub-pris-g .pub-i")).to_have_count(2)

        # Hors ligne, rien n'est parti.
        etat = page.request.get(f"{server}{lien}/etat").json()
        assert not set(cles) & set(etat["checked"])

        # Retour du réseau : la file d'attente est rejouée.
        with pub.expect_response(lambda r: r.url.endswith("/check")):
            ctx.set_offline(False)
        fin = time.monotonic() + 10
        while not set(cles) <= set(page.request.get(f"{server}{lien}/etat").json()["checked"]):
            assert time.monotonic() < fin, "coches hors ligne non enregistrées"
            time.sleep(0.2)
        expect(pub.locator("#pub-offline")).to_be_hidden()
    finally:
        ctx.close()
    assert not erreurs, erreurs


def test_une_seule_version_par_fichier_statique(page: Page, browser: Browser, server):
    _, lien = _liste_partagee(page, server, "2026-12-21")
    ctx = _contexte_sw(browser)
    pub = ctx.new_page()
    try:
        pub.goto(server + lien)
        _sw_actif(pub)
        pub.reload()  # page désormais contrôlée : ses fetch passent par le service worker
        assert pub.evaluate("!!navigator.serviceWorker.controller")

        # Fichier que la page ne charge pas elle-même : seules nos deux visites le rangent.
        fichier = "/static/js/rating.js"
        for v in ("A", "B"):
            assert pub.evaluate("(u) => fetch(u).then(r => r.ok)", f"{fichier}?v={v}")
        assert _attendre(pub, """async ([nom, chemin]) => {
            const c = await caches.open(nom);
            const urls = (await c.keys()).map(r => r.url).filter(u => new URL(u).pathname === chemin);
            return urls.length === 1 && urls[0].endsWith('?v=B');
        }""", [CACHE, fichier]), _cles_du_cache(pub)

        # Aucun fichier statique en double dans tout le cache (une fois les
        # rafraîchissements en arrière-plan de la page terminés).
        assert _attendre(pub, """async (nom) => {
            const c = await caches.open(nom);
            const chemins = (await c.keys()).map(r => new URL(r.url))
                .filter(u => u.pathname.startsWith('/static/')).map(u => u.pathname);
            return chemins.length === new Set(chemins).size;
        }""", CACHE), _cles_du_cache(pub)
    finally:
        ctx.close()


def test_page_privee_non_gardee_hors_ligne(page: Page, browser: Browser, server):
    pid, _ = _liste_partagee(page, server, "2026-12-28")
    ctx = _contexte_sw(browser)
    priv = ctx.new_page()
    try:
        priv.goto(f"{server}/planning/{pid}")
        _sw_actif(priv)
        priv.reload()  # visite contrôlée par le service worker
        assert priv.evaluate("!!navigator.serviceWorker.controller")
        expect(priv.locator("#shop-card")).to_be_visible()
        assert not [u for u in _cles_du_cache(priv) if "/planning/" in u]

        ctx.set_offline(True)
        priv.reload()
        expect(priv).to_have_title("Hors ligne — Menu Planner")
        expect(priv.locator("#shop-card")).to_have_count(0)
    finally:
        ctx.close()
