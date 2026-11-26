"""L08 — templates découpés en partiels, JS et CSS sortis en fichiers statiques."""

import json
import re
from pathlib import Path

from app import main
from tests.aides import _case, _construire, _recipe

APP_DIR = Path(main.__file__).resolve().parent
TEMPLATES = APP_DIR / "templates"
STATIC = APP_DIR / "static"
ASSET_RE = re.compile(r'(?:src|href)="(/static/(?:js|css)/[^"?]+)')


def _template_assets() -> set[str]:
    found = set()
    for f in TEMPLATES.rglob("*.html"):
        found |= set(ASSET_RE.findall(f.read_text(encoding="utf-8")))
    return found


def test_chaque_script_et_css_reference_repond_200(client):
    assets = _template_assets()
    for name in ("common.js", "planning-week.js", "planning-shopping.js", "builder.js",
                 "builder-picker.js"):
        assert f"/static/js/{name}" in assets, name
    assert "/static/css/builder.css" in assets
    for url in sorted(assets):
        r = client.get(url)
        assert r.status_code == 200, url
        assert r.text.strip(), url


def test_scripts_charges_avec_version(client):
    for f in TEMPLATES.rglob("*.html"):
        src = f.read_text(encoding="utf-8")
        for m in re.finditer(r'<script src="(/static/js/[^"]+)"', src):
            assert "?v={{ asset_version }}" in m.group(1), (f.name, m.group(1))


def test_gabarits_courts_et_sans_onclick():
    for name in ("planning.html", "index.html"):
        assert len((TEMPLATES / name).read_text(encoding="utf-8").splitlines()) < 120, name
    files = [TEMPLATES / "planning.html", TEMPLATES / "index.html",
             TEMPLATES / "courses_public.html", *sorted((TEMPLATES / "partials").glob("*.html"))]
    for f in files:
        assert "onclick=" not in f.read_text(encoding="utf-8"), f.name


def _page_data(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="page-data">(.*?)</script>', html, re.DOTALL)
    assert m, "page-data absent"
    return json.loads(m.group(1))


def test_page_data_planning(client, monkeypatch):
    poulet = _recipe("Poulet </script><b>", id="p1")

    async def _all():
        return [poulet]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    pid = _construire(client, [_case(1, "midi", poulet, persons=2)])
    html = client.get(f"/planning/{pid}").text
    data = _page_data(html)
    assert data["pid"] == pid
    assert len(data["day_labels"]) == 7 and data["day_labels"][0]["court"]
    assert "courses_checked" not in data       # sans lecteur côté JS (M05)
    # Aucune interpolation Jinja restante dans du JS inline.
    assert "const pid" not in html and "{{" not in html


def test_page_data_constructeur_echappe(client, monkeypatch):
    poulet = _recipe("Poulet </script><b>x</b>", id="p1")

    async def _all():
        return [poulet]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    r = client.post("/construire", data={
        "week_start": "2026-01-05", "action": "brouillon",
        "meals": json.dumps([_case(1, "midi", poulet, persons=2)]),
    }, follow_redirects=False)
    html = client.get(r.headers["location"]).text
    assert "</script><b>" not in html          # nom piégé neutralisé dans le JSON
    data = _page_data(html)
    assert data["saved"] is True
    assert data["builder"][0]["main"]["nom"] == "Poulet </script><b>x</b>"
    assert _page_data(client.get("/").text) == {"builder": [], "saved": False, "valide": False, "nb_jours": 0}


def test_fonctions_pures_du_constructeur_exposees():
    src = (STATIC / "js" / "builder.js").read_text(encoding="utf-8")
    assert "window.Builder = {" in src
    for fn in ("consolidateGroups", "midiEditorDay", "serialize", "rangeDays", "hydrateBuilder"):
        assert re.search(rf"function {fn}\(state|function {fn}\(", src), fn
    common = (STATIC / "js" / "common.js").read_text(encoding="utf-8")
    # Une seule définition de l'API commune (plus d'ajout par Object.assign).
    api = "esc, copyText, requestJson, postJson, modal, confirmModal, sessionExpired, pageData, qtyFr"
    assert f"window.Common = {{{api}}};" in common
    assert common.count("window.Common =") == 1 and "Object.assign(window.Common" not in common


def test_page_publique_utilise_common(client, monkeypatch):
    riz = _recipe("Riz", id="r1")

    async def _all():
        return [riz]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    pid = _construire(client, [_case(1, "midi", riz, persons=2)])
    url = client.post(f"/planning/{pid}/partager").json()["url"]
    html = client.get(url).text
    assert '<script src="/static/js/common.js?v=' in html
    assert "document.execCommand" not in html   # copie mutualisée dans common.js
