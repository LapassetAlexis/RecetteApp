"""L05 — gabarits planning / constructeur : en-tête, modales, navigation, jetons CSS."""

import re
from pathlib import Path

import pytest

from app import main
from tests.aides import _case, _construire, _recipe

APP_DIR = Path(main.__file__).resolve().parent
STYLE = APP_DIR / "static" / "style.css"
TEMPLATES = APP_DIR / "templates"


def _with_scripts(client, html: str) -> str:
    """HTML de la page + contenu des scripts locaux qu'elle charge (JS sorti en fichiers, L08)."""
    srcs = re.findall(r'<script src="(/static/js/[^"?]+)', html)
    return html + "".join(client.get(s).text for s in srcs)


@pytest.fixture
def planning_id(client, monkeypatch):
    poulet = _recipe('Salade "César" <b>x</b>', id="p1")
    soupe = _recipe("Soupe", id="s1")

    async def _all():
        return [poulet, soupe]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    return _construire(client, [_case(1, "midi", poulet, persons=2),
                                _case(1, "soir", soupe, persons=2)])


def test_entete_planning_sans_saison(client, planning_id):
    html = client.get(f"/planning/{planning_id}").text
    assert "—  —" not in html
    assert "pers." in html
    # Reliquats de la génération IA retirés.
    assert "hero-force" not in html
    assert "Certains créneaux n'ont pas pu" not in html


def test_nom_recette_echappe_cote_serveur(client, planning_id):
    html = client.get(f"/planning/{planning_id}").text
    assert "<b>x</b>" not in html
    assert ("Salade &#34;César&#34; &lt;b&gt;x&lt;/b&gt;" in html
            or "Salade &quot;César&quot; &lt;b&gt;x&lt;/b&gt;" in html)


def test_modale_planning_accessible(client, planning_id):
    html = _with_scripts(client, client.get(f"/planning/{planning_id}").text)
    assert 'role="dialog"' in html and 'aria-modal="true"' in html
    assert 'aria-labelledby="modal-title"' in html and 'id="modal-title"' in html
    assert "Escape" in html
    # Alternatives construites en DOM (plus de nom injecté dans innerHTML).
    assert "el.innerHTML=alts.map" not in html
    assert "textContent=a.nom" in html
    assert "function usl" not in html


def test_liste_vide_message(client, planning_id):
    html = client.get(f"/planning/{planning_id}").text
    assert "Aucun ingrédient pour l'instant" in html
    assert "Planning validé !" not in html
    assert 'id="btn-shop"' in html


def test_historique_sans_saison(client, planning_id):
    assert client.post(f"/planning/{planning_id}/valider").json().get("success")
    html = client.get("/historique").text
    assert "· 2 pers." not in html
    assert "2 pers." in html


def test_historique_vide(client):
    html = client.get("/historique").text
    assert "Aucun planning pour le moment" in html
    assert "Construire une semaine" in html


def test_constructeur_modale_et_navigation(client):
    html = _with_scripts(client, client.get("/").text)
    assert 'role="dialog"' in html and 'aria-labelledby="pk-title"' in html
    assert "Escape" in html
    assert re.search(r"(?<![\w.])alert\('", html) is None
    assert ">Planifier</a>" in html and ">Générer</a>" not in html
    assert '/static/icon-180.png' in html
    # Onglet actif « Planifier » sur / uniquement.
    nav = re.search(r'<div class="nav-links">(.*?)</div>', html, re.DOTALL).group(1)
    assert re.search(r'class="active"[^>]*>Planifier<', nav)
    assert not re.search(r'class="active"[^>]*>Historique<', nav)


def test_onglet_actif_planning(client, planning_id):
    html = client.get(f"/planning/{planning_id}").text
    nav = re.search(r'<div class="nav-links">(.*?)</div>', html, re.DOTALL).group(1)
    assert re.search(r'class="active"[^>]*>Historique<', nav)
    assert not re.search(r'class="active"[^>]*>Planifier<', nav)


def _root_vars():
    css = STYLE.read_text(encoding="utf-8")
    root = re.search(r":root\s*\{(.*?)\}", css, re.DOTALL).group(1)
    return set(re.findall(r"(--[\w-]+)\s*:", root))


def test_variables_css_toutes_definies():
    defined = _root_vars()
    files = [STYLE, *sorted(TEMPLATES.glob("*.html"))]
    missing = {}
    for f in files:
        used = set(re.findall(r"var\(\s*(--[\w-]+)", f.read_text(encoding="utf-8")))
        if used - defined:
            missing[f.name] = sorted(used - defined)
    assert not missing, f"variables CSS non définies dans :root : {missing}"


def test_css_mort_et_group_picker_global_retires():
    css = STYLE.read_text(encoding="utf-8")
    for cls in (".day-group", ".midi-grid", ".nb-input", ".meal-toggles", ".preview-",
                ".cook-render", ".cook-step", ".hero-force", ".group-picker"):
        assert cls not in css, cls
    assert ":focus-visible" in css
