"""Service worker (M04, N9) : contrôles statiques de `sw.js`.

Le comportement réel (purge des versions `?v=`, pages privées non gardées,
liste publique hors ligne) est vérifié dans un vrai navigateur par
`tests/e2e/test_parcours_hors_ligne.py`."""

import re
from pathlib import Path

SW = (Path(__file__).resolve().parents[1] / "app" / "static" / "sw.js").read_text(encoding="utf-8")


def _bloc(debut: str, fin: str) -> str:
    i = SW.index(debut)
    return SW[i:SW.index(fin, i)]


def test_nom_de_cache_incremente():
    assert "const CACHE = 'menu-planner-v6'" in SW


def test_borne_de_pages():
    assert re.search(r"const MAX_PAGES = 20;", SW)


def test_purge_des_autres_versions_du_meme_chemin():
    put_static = _bloc("function putStatic", "\n}\n")
    assert "u.pathname === url.pathname && u.href !== url.href" in put_static
    assert "cache.delete(k)" in put_static
    # Les assets passent tous par putStatic, plus de cache.put direct sur la requête.
    assert "cache.put(req, res.clone())" not in SW
    assert "putStatic(cache, req, res.clone())" in SW


def test_seule_la_liste_publique_est_mise_en_cache_a_la_navigation():
    nav = SW[SW.index("// Navigations / pages"):]
    assert "PAGE_RE.test(url.pathname)" in nav
    assert "if (!publique) return res;" in nav
    # Plus aucune mise en cache de la requête de navigation telle quelle.
    assert "c.put(req, copy)" not in SW
    assert "hit || caches.match(OFFLINE_URL)" in nav


def test_motifs_des_urls_publiques():
    page_re = re.search(r"const PAGE_RE = /(.+)/;", SW).group(1)
    etat_re = re.search(r"const ETAT_RE = /(.+)/;", SW).group(1)
    js_vers_py = lambda motif: re.compile(motif.replace(r"\/", "/"))  # noqa: E731
    page, etat = js_vers_py(page_re), js_vers_py(etat_re)
    assert page.match("/courses/abc_-12") and page.match("/courses/abc/")
    for prive in ("/", "/planning/3", "/courses", "/courses/abc/etat", "/admin", "/recettes/x"):
        assert not page.match(prive), prive
    assert etat.match("/courses/abc/etat") and not etat.match("/courses/abc")


def test_borne_retire_les_plus_anciennes_avec_leur_etat():
    put_page = _bloc("function putPage", "\n}\n")
    assert "pages.slice(0, Math.max(0, pages.length - MAX_PAGES))" in put_page
    assert "'/etat'" in put_page


def test_cache_a_la_demande_filtre_les_urls():
    store = _bloc("function store", "\n}\n")
    assert "startsWith('/static/')" in store
    assert "PAGE_RE.test(url.pathname) || ETAT_RE.test(url.pathname)" in store
    assert store.rstrip().endswith("return Promise.resolve();")
    message = _bloc("addEventListener('message'", "\n});\n")
    assert "store(cache, u, res)" in message
    assert "cache.put(u, res)" not in message
