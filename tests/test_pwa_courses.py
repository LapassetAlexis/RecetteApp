"""L15 — liste de courses publique utilisable hors ligne.

Le comportement navigateur (service worker, file d'attente, rejeu) est vérifié
à la main dans Chrome (CDP, réseau coupé) ; ici : le contrat serveur sur lequel
repose le rejeu (coche idempotente, état lisible), et les invariants des
fichiers statiques."""

import re
from pathlib import Path

import pytest

import app.main as main
from tests.test_courses_partage import LAIT_2_LIGNES, make_planning, share

APP = Path(main.__file__).parent
SW = (APP / "static" / "sw.js").read_text(encoding="utf-8")
JS = (APP / "static" / "js" / "courses-public.js").read_text(encoding="utf-8")
TPL = (APP / "templates" / "courses_public.html").read_text(encoding="utf-8")


@pytest.fixture
def lien(client, monkeypatch):
    pid = make_planning(client, monkeypatch, LAIT_2_LIGNES)
    token = share(client, pid).rstrip("/").rsplit("/", 1)[1]
    return f"/courses/{token}"


def _etat(client, lien):
    r = client.get(f"{lien}/etat")
    assert r.status_code == 200
    return r.json()


# ── Contrat serveur du rejeu ────────────────────────────────────────────

def test_coche_rejouee_deux_fois_ne_bascule_pas(client, lien):
    item = "lait|volume"
    for _ in range(2):
        assert client.post(f"{lien}/check", json={"item": item, "checked": True}).json()["success"]
        assert item in _etat(client, lien)["checked"]


def test_decoche_rejouee_deux_fois_reste_decochee(client, lien):
    item = "lait|volume"
    client.post(f"{lien}/check", json={"item": item, "checked": True})
    for _ in range(2):
        assert client.post(f"{lien}/check", json={"item": item, "checked": False}).status_code == 200
        assert item not in _etat(client, lien)["checked"]


def test_file_rejouee_dans_le_desordre_garde_la_derniere_valeur(client, lien):
    """La file ne garde qu'un état par article : seule la dernière valeur part."""
    etat0 = _etat(client, lien)
    client.post(f"{lien}/check", json={"item": "lait|volume", "checked": True})
    client.post(f"{lien}/check", json={"item": "lait|pot", "checked": True})
    etat = _etat(client, lien)
    assert {"lait|volume", "lait|pot"} <= set(etat["checked"])
    assert etat["version"] == etat0["version"]   # cocher ne recharge pas la page


def test_cle_inconnue_rejetee_400(client, lien):
    """La file abandonne l'article et recharge la page sur un 400."""
    assert client.post(f"{lien}/check", json={"item": "inconnu|", "checked": True}).status_code == 400


# ── Service worker ──────────────────────────────────────────────────────

def _precache() -> list[str]:
    m = re.search(r"const PRECACHE = \[(.*?)\];", SW, re.S)
    return re.findall(r"'([^']+)'", m.group(1))


def test_cache_incremente():
    assert "const CACHE = 'menu-planner-v6'" in SW  # M04


def test_precache_assets_de_la_page_publique(client):
    for path in ("/static/css/courses.css", "/static/js/common.js", "/static/js/courses-public.js"):
        assert path in _precache()
        assert client.get(path).status_code == 200, path


def test_page_publique_reference_les_assets_precaches():
    for path in ("/static/css/courses.css", "/static/js/common.js", "/static/js/courses-public.js"):
        assert f'"{path}?v=' in TPL


def test_etat_reseau_puis_cache_et_marque():
    assert "ETAT_RE" in SW and "/etat$/" in SW
    assert "X-SW-Cache" in SW and "X-SW-Cache" in JS


def test_mise_en_cache_a_la_demande_meme_origine():
    assert "'cache-urls'" in SW and "'cache-urls'" in JS
    assert "self.location.origin" in SW


def test_sw_js_public_et_toujours_servi(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert r.headers.get("service-worker-allowed") == "/"


# ── Page publique ───────────────────────────────────────────────────────

def test_bandeau_hors_ligne_masque_par_defaut(client, lien):
    html = client.get(lien).text
    m = re.search(r'<p[^>]*id="pub-offline"[^>]*>(.*?)</p>', html, re.S)
    assert m and " hidden" in m.group(0)
    assert "tes coches seront envoyées au retour du réseau" in m.group(1)


def test_js_file_attente_et_degradation_http():
    # Enregistrement du SW seulement en contexte sécurisé : rien en HTTP simple.
    assert "window.isSecureContext" in JS and "'serviceWorker' in navigator" in JS
    # localStorage toujours protégé.
    for call in re.findall(r".*localStorage\.\w+\(.*", JS):
        assert call.strip().startswith("try {"), call
    # Rejeu : événement online + chargement ; état voulu envoyé, pas une bascule.
    assert "addEventListener('online'" in JS
    assert "JSON.stringify({item, checked})" in JS
    assert "cb.checked = !cb.checked" not in JS
