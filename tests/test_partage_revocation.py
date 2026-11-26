"""M06 (D8) — lien de partage révocable et régénérable, limite des coches
publiques, oubli du lien révoqué par la page publique et le service worker."""

import asyncio
import re
from pathlib import Path

import app.main as main
from app.routers import courses as courses_router
from app.services.shopping_list import SHARE_TOKEN_BYTES, share_action
from tests.test_courses_partage import LAIT_2_LIGNES, data_of, make_planning, share

APP = Path(main.__file__).parent
SW = (APP / "static" / "sw.js").read_text(encoding="utf-8")
JS_PUB = (APP / "static" / "js" / "courses-public.js").read_text(encoding="utf-8")
JS_PLANNING = (APP / "static" / "js" / "planning-shopping.js").read_text(encoding="utf-8")
TPL_PUB = (APP / "templates" / "courses_public.html").read_text(encoding="utf-8")


def _token(url: str) -> str:
    return url.rsplit("/", 1)[1]


def _trois_routes(client, url: str) -> list[int]:
    return [client.get(url).status_code,
            client.get(f"{url}/etat").status_code,
            client.post(f"{url}/check", json={"item": "lait|volume", "checked": True}).status_code]


# ── Service ────────────────────────────────────────────────────────────

def test_share_action():
    data = {}
    tok = share_action(data)
    assert tok and data["share_token"] == tok
    assert share_action(data) == tok                       # idempotent
    tok2 = share_action(data, "regenerer")
    assert tok2 != tok and data["share_token"] == tok2
    assert share_action(data, "revoquer") is None and "share_token" not in data
    assert share_action(data, "revoquer") is None


def test_nouveau_jeton_de_16_octets():
    # token_urlsafe(16) : 16 octets en base64 url → 22 caractères.
    assert SHARE_TOKEN_BYTES == 16
    assert len(share_action({})) == 22


# ── Routes ─────────────────────────────────────────────────────────────

def test_lien_revoque_404_sur_les_trois_routes(client, monkeypatch):
    """Critère : un lien révoqué répond 404 sur les trois routes publiques."""
    pid = make_planning(client, monkeypatch, LAIT_2_LIGNES)
    url = share(client, pid)
    assert _trois_routes(client, url) == [200, 200, 200]

    r = client.post(f"/planning/{pid}/partager", json={"action": "revoquer"})
    assert r.status_code == 200 and r.json() == {"url": None}
    assert "share_token" not in data_of(pid)
    assert _trois_routes(client, url) == [404, 404, 404]
    # La colonne indexée suit : plus aucun planning pour ce jeton.
    assert asyncio.run(main.db.get_planning_by_share_token(_token(url))) is None


def test_reponses_404_reconnaissables_par_la_page(client):
    page = client.get("/courses/revoque")
    assert page.status_code == 404
    assert "Ce lien n'est plus valide" in page.text
    assert '{"revoque": true}' in page.text
    assert 'id="pub-revoque"' in page.text and 'id="pub-revoque" hidden' not in page.text
    for r in (client.get("/courses/revoque/etat"),
              client.post("/courses/revoque/check", json={"item": "x|", "checked": True})):
        assert r.status_code == 404 and r.json()["code"] == "revoque"


def test_page_publique_normale_cache_le_message(client, monkeypatch):
    pid = make_planning(client, monkeypatch, LAIT_2_LIGNES)
    html = client.get(share(client, pid)).text
    assert 'id="pub-revoque" hidden' in html and 'id="pub-liste"' in html
    assert '"version":' in html.replace(" ", "")


def test_regenerer_donne_un_nouveau_lien(client, monkeypatch):
    pid = make_planning(client, monkeypatch, LAIT_2_LIGNES)
    ancien = share(client, pid)
    nouveau = client.post(f"/planning/{pid}/partager", json={"action": "regenerer"}).json()["url"]
    assert nouveau != ancien and nouveau.startswith("/courses/")
    assert len(_token(nouveau)) == 22
    assert client.get(ancien).status_code == 404
    assert client.get(nouveau).status_code == 200
    # Partager sans action redonne le lien actif.
    assert share(client, pid) == nouveau


def test_partager_apres_revocation_cree_un_nouveau_lien(client, monkeypatch):
    pid = make_planning(client, monkeypatch, LAIT_2_LIGNES)
    ancien = share(client, pid)
    client.post(f"/planning/{pid}/partager", json={"action": "revoquer"})
    nouveau = share(client, pid)
    assert nouveau != ancien and client.get(nouveau).status_code == 200


def test_action_inconnue_400(client, monkeypatch):
    pid = make_planning(client, monkeypatch, LAIT_2_LIGNES)
    r = client.post(f"/planning/{pid}/partager", json={"action": "detruire"})
    assert r.status_code == 400
    assert "share_token" not in data_of(pid)


def test_revoquer_sans_lien_ne_fait_rien(client, monkeypatch):
    pid = make_planning(client, monkeypatch, LAIT_2_LIGNES)
    assert client.post(f"/planning/{pid}/partager", json={"action": "revoquer"}).json() == {"url": None}


def test_coches_conservees_apres_regeneration_du_lien(client, monkeypatch):
    pid = make_planning(client, monkeypatch, LAIT_2_LIGNES)
    url = share(client, pid)
    client.post(f"{url}/check", json={"item": "lait|volume", "checked": True})
    nouveau = client.post(f"/planning/{pid}/partager", json={"action": "regenerer"}).json()["url"]
    assert client.get(f"{nouveau}/etat").json()["checked"] == ["lait|volume"]


# ── Limite des coches publiques ────────────────────────────────────────

def test_fenetre_glissante():
    courses_router._check_hits.pop("tok-test", None)
    lim = courses_router.CHECK_LIMIT
    assert lim == 120
    assert all(courses_router._check_autorise("tok-test", now=100.0 + i * 0.1) for i in range(lim))
    assert not courses_router._check_autorise("tok-test", now=115.0)
    # Une minute après la première coche, la place se libère.
    assert courses_router._check_autorise("tok-test", now=160.0)
    # Un autre jeton n'est pas concerné.
    assert courses_router._check_autorise("tok-autre", now=115.0)


def test_429_au_dela_de_la_limite(client, monkeypatch):
    pid = make_planning(client, monkeypatch, LAIT_2_LIGNES)
    url = share(client, pid)
    monkeypatch.setattr(courses_router, "CHECK_LIMIT", 3)
    codes = [client.post(f"{url}/check", json={"item": "lait|volume", "checked": True}).status_code
             for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    assert client.post(f"{url}/check", json={"item": "lait|volume", "checked": True}).json()["code"] == "limite"


# ── Page publique : oubli du lien révoqué ───────────────────────────────

def test_page_publique_vide_sa_file_sur_404():
    revoked = JS_PUB[JS_PUB.index("function revoked()"):JS_PUB.index("\n    }\n", JS_PUB.index("function revoked()"))]
    assert "lsDel(QKEY)" in revoked and "lsDel(SKEY)" in revoked and "memQueue = {}" in revoked
    assert "'forget-page'" in revoked
    assert "if (r.status === 404) { revoked(); return; }" in JS_PUB          # coche
    assert "if (r.status === 404) { revoked(); return false; }" in JS_PUB    # état
    assert "if (pageData().revoque) { revoked(); return; }" in JS_PUB        # page 404
    # 429 : la file est gardée (pas de suppression de l'entrée).
    assert "if (r.status === 429) return;" in JS_PUB


def test_style_inline_deplace_dans_courses_css():
    assert "<style" not in TPL_PUB
    css = (APP / "static" / "css" / "courses.css").read_text(encoding="utf-8")
    assert ".pub-offline {" in css


# ── Service worker : lien révoqué retiré du cache ──────────────────────

def test_sw_oublie_un_lien_revoque():
    assert "const CACHE = 'menu-planner-v6'" in SW
    forget = SW[SW.index("function forgetPage"):SW.index("\n}\n", SW.index("function forgetPage"))]
    assert "c.delete(page + '/etat')" in forget and "c.delete(page)" in forget
    # 404 réseau sur la liste ou son état → oubli.
    assert SW.count("res.status === 404") == 2
    assert SW.count("event.waitUntil(forgetPage(url.pathname))") == 2
    # Message de la page, filtré sur les URL publiques.
    message = SW[SW.index("addEventListener('message'"):]
    assert "data.type === 'forget-page'" in message
    assert "PAGE_RE.test(data.path) || ETAT_RE.test(data.path)" in message


def test_sw_forget_page_retrouve_la_page_depuis_l_etat():
    expr = re.search(r"pathname\.replace\((/.+?/), ''\)\.replace\((/.+?/), ''\)", SW)
    assert expr, "normalisation du chemin absente"
    py = [re.compile(e.strip("/").replace(r"\/", "/")) for e in expr.groups()]
    for chemin in ("/courses/abc", "/courses/abc/", "/courses/abc/etat"):
        assert py[1].sub("", py[0].sub("", chemin)) == "/courses/abc"


# ── Page planning : JS ──────────────────────────────────────────────────

def test_planning_shopping_passe_par_request_json():
    assert "requestJson(url, {method: method || 'POST', body})" in JS_PLANNING
    assert "fetch(" not in JS_PLANNING
    assert "{action: 'revoquer'}" in JS_PLANNING
    assert "`/api/courses/${pid}/etat`" in JS_PLANNING and "REFRESH_MS = 20000" in JS_PLANNING
    assert "document.visibilityState !== 'visible'" in JS_PLANNING
    # Reprise de l'ancienne file (liste) au format objet {clé: état}.
    assert "Array.isArray(q)" in JS_PLANNING
    # Confirmation avant de régénérer une liste cochée.
    assert "window.confirm(" in JS_PLANNING[JS_PLANNING.index("async function genShop"):]
