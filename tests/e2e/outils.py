"""Aides communes aux parcours e2e : création de plannings par HTTP et gestes
du constructeur (picker, menu « ⋯ »)."""

import json
import re
from datetime import date, timedelta
from urllib.parse import quote

from playwright.sync_api import Browser, Page, expect

from tests.e2e.fake_notion import RECETTES

_PAR_ID = {r["id"]: r for r in RECETTES}


def case(jour, moment, main=None, persons=2, group=None, accs=()):
    """Case du champ `meals` du constructeur ; `main` et `accs` = ids du faux catalogue."""
    c = {"jour": jour, "moment": moment, "persons": persons}
    if moment == "midi":
        c["group"] = group if group is not None else jour
    if main:
        r = _PAR_ID[main]
        c["main"] = {"notion_id": r["id"], "nom": r["nom"], "nature": r["nature"]}
    c["accompagnements"] = [{"notion_id": a, "nom": _PAR_ID[a]["nom"], "nature": _PAR_ID[a]["nature"]}
                            for a in accs]
    return c


def semaine(nb_jours, repas, persons=2):
    """14 cases : `repas` = {"jour:moment": id ou (id, group)} ; jours hors
    période (> nb_jours) émis absents comme le fait le constructeur."""
    out = []
    for j in range(1, 8):
        for m in ("midi", "soir"):
            v = repas.get(f"{j}:{m}")
            rid, group = (v if isinstance(v, tuple) else (v, None))
            out.append(case(j, m, rid, persons if j <= nb_jours else 0, group))
    return out


def construire(page: Page, server: str, cases, week_start: str, action="valider", draft_id="") -> int:
    """POST /construire (comme le formulaire) ; renvoie l'id du planning."""
    r = page.request.post(server + "/construire", form={
        "week_start": week_start, "meals": json.dumps(cases), "action": action, "draft_id": draft_id,
    }, max_redirects=0)
    loc = r.headers["location"]
    return int(re.search(r"(?:planning/|draft=|planning=)(\d+)", loc).group(1))


def valider(page: Page, server: str, pid: int) -> None:
    assert page.request.post(f"{server}/planning/{pid}/valider").json()["success"]


def semaine_validee(page: Page, server: str, cases, week_start: str) -> int:
    pid = construire(page, server, cases, week_start)
    valider(page, server, pid)
    return pid


def prochain(weekday: int, apres: date | None = None) -> date:
    """Prochaine date (strictement après `apres`) tombant le jour `weekday` (0 = lundi)."""
    d = (apres or date.today()) + timedelta(days=1)
    return d + timedelta(days=(weekday - d.weekday()) % 7)


# ── Constructeur ─────────────────────────────────────────────────────────

def cellule(page: Page, key: str):
    return page.locator(f'.b-cell[data-key="{key}"]')


def _choisir_dans_picker(page: Page, recherche: str, nom: str) -> None:
    expect(page.locator("#pk")).to_have_class(re.compile(r"\bopen\b"))
    page.fill("#pk-search", recherche)
    carte = page.locator(".pk-card").filter(has=page.locator(".recipe-title", has_text=nom))
    carte.first.click()
    expect(page.locator("#pk")).not_to_have_class(re.compile(r"\bopen\b"))


def choisir_plat(page: Page, key: str, nom: str, recherche: str | None = None) -> None:
    cellule(page, key).locator('[data-act="pick-main"]').click()
    _choisir_dans_picker(page, recherche or nom, nom)
    expect(cellule(page, key).locator(".b-main")).to_contain_text(nom)


def ajouter_accompagnement(page: Page, key: str, nom: str) -> None:
    cellule(page, key).locator('[data-act="add-side"]').click()
    _choisir_dans_picker(page, nom, nom)
    expect(cellule(page, key).locator(".b-side")).to_contain_text([nom])


def periode(page: Page, du: str, au: str) -> None:
    """Remplit « Du » puis « Au » (fill déclenche aussi l'événement change)."""
    page.fill("#week_start", du)
    page.fill("#week_end", au)


def pas_de_defilement_horizontal(page: Page) -> None:
    larges = page.evaluate("""() => {
        const vw = document.documentElement.clientWidth;
        return {scroll: document.documentElement.scrollWidth, vw};
    }""")
    assert larges["scroll"] <= larges["vw"], f"défilement horizontal : {larges}"


# ── Contexte dédié et connexion (serveur avec auth) ──────────────────────

def contexte(browser: Browser, largeur: int):
    """Contexte neuf (390 px = mobile tactile), service worker bloqué ; renvoie
    (contexte, page, erreurs JS ou console hors refus HTTP attendus)."""
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


def connexion(page: Page, server: str, mot_de_passe="secret", chemin="/historique"):
    """Ouvre `chemin`, suit la redirection vers /login et se connecte (chef)."""
    page.goto(server + chemin)
    expect(page).to_have_url(server + "/login?next=" + quote(chemin, safe=""))
    page.fill('input[name="username"]', "chef")
    page.fill('input[name="password"]', mot_de_passe)
    page.locator('button[type="submit"]').click()
