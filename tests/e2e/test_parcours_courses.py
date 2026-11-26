"""Parcours de la liste de courses : génération, coches par ligne (B7),
article ajouté à la main, lien public ouvert dans un second contexte, coches
synchronisées dans les deux sens (L09)."""

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.outils import semaine, semaine_validee

pytestmark = pytest.mark.e2e


def _ligne(page: Page, texte: str):
    return page.locator("#shop-g .shop-i").filter(has=page.locator("span", has_text=texte))


def _sondage(page: Page) -> None:
    """Déclenche le rafraîchissement de la page publique (sinon toutes les 20 s)."""
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")


def test_liste_coches_partage_public(page: Page, browser: Browser, server):
    # Quiche (tomates à la pièce) + bœuf (tomates en grammes) : deux lignes « Tomates ».
    cases = semaine(2, {"1:midi": "g1", "1:soir": "q1", "2:soir": "b1"})
    pid = semaine_validee(page, server, cases, "2026-11-02")
    page.goto(f"{server}/planning/{pid}")

    # (Re)génération de la liste depuis la page.
    with page.expect_navigation():
        page.click("#btn-regen")
    tomates = _ligne(page, "Tomates")
    expect(tomates).to_have_count(2)
    cles = tomates.evaluate_all("els => els.map(e => e.dataset.item)")
    assert len(set(cles)) == 2, f"clés de coche identiques : {cles}"  # B7

    # Cocher une seule des deux lignes : seule celle-ci reste cochée au rechargement.
    # Clés d'article : nom + famille d'unité (pièce / masse).
    piece = page.locator('#shop-g .shop-i[data-item="tomate|"]')
    grammes = page.locator('#shop-g .shop-i[data-item="tomate|masse"]')
    with page.expect_response(lambda r: "/api/shopping-check/" in r.url):
        piece.locator(".shop-c").check()
    page.reload()
    expect(piece.locator(".shop-c")).to_be_checked()
    expect(grammes.locator(".shop-c")).not_to_be_checked()

    # Article ajouté à la main.
    page.fill("#shop-add-t", "2 l jus d'orange")
    page.press("#shop-add-t", "Enter")
    jus = page.locator("#shop-g .shop-i.shop-extra").filter(has_text="Jus d'orange")
    expect(jus).to_have_count(1)

    # Lien public.
    with page.expect_response(lambda r: r.url.endswith(f"/planning/{pid}/partager")) as rep:
        page.click("#btn-share")
    lien = rep.value.json()["url"]
    assert lien.startswith("/courses/")

    ctx = browser.new_context(viewport={"width": 390, "height": 844}, service_workers="block")
    erreurs: list[str] = []
    pub = ctx.new_page()
    pub.on("pageerror", lambda e: erreurs.append(str(e)))
    try:
        pub.goto(server + lien)
        # La ligne cochée est rangée sous « Déjà pris », l'autre « Tomates » reste à prendre.
        expect(pub.locator("#pub-pris-g .shop-i").filter(has_text="Tomates")).to_have_count(1)
        expect(pub.locator("#shop-g .shop-i").filter(has_text="Tomates")).to_have_count(1)
        jus_pub = pub.locator(".shop-i").filter(has_text="Jus d'orange")
        expect(jus_pub).to_have_count(1)

        # Coche depuis le lien public → visible sur la page planning.
        with pub.expect_response(lambda r: r.url.endswith("/check")) as rep:
            jus_pub.locator(".shop-c").check()
        assert rep.value.ok
        expect(pub.locator("#pub-pris-g .shop-i").filter(has_text="Jus d'orange")).to_have_count(1)
        page.reload()
        expect(jus.locator(".shop-c")).to_be_checked()

        # Coche depuis la page planning → reprise par la page publique au rafraîchissement.
        with page.expect_response(lambda r: "/api/shopping-check/" in r.url):
            grammes.locator(".shop-c").check()
        _sondage(pub)
        expect(pub.locator("#pub-pris-g .shop-i").filter(has_text="Tomates")).to_have_count(2)
        expect(pub.locator("#shop-g .shop-i").filter(has_text="Tomates")).to_have_count(0)

        # Décocher depuis le lien public (section « Déjà pris » dépliée) → décoché sur la page planning.
        pub.click("#pub-pris summary")
        with pub.expect_response(lambda r: r.url.endswith("/check")):
            pub.locator('#pub-pris-g .shop-i[data-item="tomate|"] .shop-c').uncheck()
        expect(pub.locator('#shop-g .shop-i[data-item="tomate|"]')).to_have_count(1)
        page.reload()
        expect(piece.locator(".shop-c")).not_to_be_checked()
        expect(grammes.locator(".shop-c")).to_be_checked()
    finally:
        ctx.close()
    assert not erreurs, erreurs
