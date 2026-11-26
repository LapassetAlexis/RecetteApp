"""L14 — Mode cuisine : rendu de la page, mise à l'échelle des ingrédients,
détection des durées (via Node si disponible)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app import main
from tests.aides import _recipe

JS = Path(main.__file__).resolve().parent / "static" / "js" / "cuisine.js"


@pytest.fixture
def recette(monkeypatch):
    """Recette « abc » pour 4 avec deux ingrédients et deux étapes."""
    async def _get(pid):
        return _recipe("Tarte <b>x</b>", "Dessert", id="abc") if pid == "abc" else None

    async def _instr(pid):
        return ["Préchauffer le four.", "Cuire 20 min."]

    async def _enriched(pid):
        return {"base_servings": 4, "ingredients": json.dumps([
            {"nom": "farine", "quantite": "200", "unite": "g"},
            {"nom": "sel", "quantite": "une pincée", "unite": ""},
        ])}
    monkeypatch.setattr(main.notion, "get_recipe", _get)
    monkeypatch.setattr(main.notion, "get_recipe_instructions", _instr)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)


def _page_data(html: str) -> dict:
    return json.loads(html.split('id="page-data">')[1].split("</script>")[0])


def test_page_cuisine_etapes_et_ingredients(client, recette):
    r = client.get("/recette/abc/cuisine")
    assert r.status_code == 200
    html = r.text
    assert "Préchauffer le four." in html and "Cuire 20 min." in html
    assert html.count('class="ck-step"') == 2
    assert "200 g" in html and "farine" in html          # base 4 : quantité d'origine
    assert "une pincée" in html                          # non numérique : inchangé
    assert 'class="nav"' not in html                     # pas de navigation de l'app
    assert "/static/js/cuisine.js" in html and "/static/css/cuisine.css" in html
    assert "Tarte &lt;b&gt;x&lt;/b&gt;" in html          # titre échappé
    assert _page_data(html) == {"recette_id": "abc", "portions": 4, "retour": "/recette/abc"}


def test_page_cuisine_portions_mises_a_l_echelle(client, recette):
    html = client.get("/recette/abc/cuisine?portions=6").text
    assert "300 g" in html and "👤 6 personnes" in html
    html = client.get("/recette/abc/cuisine?portions=1").text
    assert "50 g" in html and "👤 1 personne" in html


@pytest.mark.parametrize("raw, attendu", [("0", 1), ("-3", 1), ("999", 50), ("abc", 4), ("", 4)])
def test_page_cuisine_portions_bornees(client, recette, raw, attendu):
    r = client.get(f"/recette/abc/cuisine?portions={raw}")
    assert r.status_code == 200
    assert _page_data(r.text)["portions"] == attendu


def test_page_cuisine_recette_inconnue(client, recette):
    assert client.get("/recette/zzz/cuisine").status_code == 404


def test_page_cuisine_sans_etapes_ni_ingredients(client, monkeypatch):
    async def _get(pid):
        return _recipe("Vide", id="v")

    async def _instr(pid):
        raise RuntimeError("Notion indisponible")

    async def _none(pid):
        return None
    monkeypatch.setattr(main.notion, "get_recipe", _get)
    monkeypatch.setattr(main.notion, "get_recipe_instructions", _instr)
    monkeypatch.setattr(main.db, "get_enriched", _none)
    r = client.get("/recette/v/cuisine")
    assert r.status_code == 200
    assert "Aucune étape enregistrée" in r.text and "Aucun ingrédient enregistré" in r.text
    assert 'id="ck-next"' not in r.text


def test_page_cuisine_erreur_interne(client, monkeypatch):
    async def _boom(pid):
        raise RuntimeError("panne")
    monkeypatch.setattr(main.notion, "get_recipe", _boom)
    assert client.get("/recette/abc/cuisine").status_code == 500


def test_fiche_propose_le_mode_cuisine(client, recette):
    html = client.get("/recette/abc").text
    assert 'id="btn-cuisine"' in html
    assert 'href="/recette/abc/cuisine?portions=4"' in html


NODE_SCRIPT = r"""
const C = require(process.argv[1]);
const cas = JSON.parse(process.argv[2]);
console.log(JSON.stringify(cas.map(t => C.parseDurations(t).map(d => [d.text, d.seconds, d.label]))));
console.log(JSON.stringify([C.formatClock(1200), C.formatClock(5400), C.formatDuration(90)]));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="Node absent")
def test_detection_des_durees_node():
    cas = ["Cuire 20 min.", "Mijoter 1 h 30 à feu doux", "Cuire 1h30", "Reposer 45 minutes",
           "Cuire 2 heures", "Cuire 10 mn puis 30 secondes", "Ajouter 2 huiles et 2 minces tranches",
           "Cuire 15 à 20 min", "200 g de farine, 180°C"]
    out = subprocess.run(["node", "-e", NODE_SCRIPT, str(JS), json.dumps(cas)],
                         capture_output=True, text=True, check=True, timeout=30).stdout.splitlines()
    assert json.loads(out[0]) == [
        [["20 min", 1200, "20 min"]],
        [["1 h 30", 5400, "1 h 30"]],
        [["1h30", 5400, "1 h 30"]],
        [["45 minutes", 2700, "45 min"]],
        [["2 heures", 7200, "2 h"]],
        [["10 mn", 600, "10 min"], ["30 secondes", 30, "30 s"]],
        [],
        [["20 min", 1200, "20 min"]],
        [],
    ]
    assert json.loads(out[1]) == ["20:00", "1:30:00", "1 min 30"]
