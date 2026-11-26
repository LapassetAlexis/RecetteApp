"""M08 — recettes et historique : JS sorti en fichiers, modales communes,
dates lisibles, édition (cache sans ingrédient, message d'échec détaillé,
« sel, poivre » découpé)."""

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import app.main as main
from tests.aides import _case, _recipe

APP_DIR = Path(main.__file__).resolve().parent
TEMPLATES = APP_DIR / "templates"
JS = APP_DIR / "static" / "js"
PAGES = ("recettes.html", "recette_detail.html", "ajouter.html", "enrichir.html", "historique.html")
NOUVEAUX_JS = ("recettes-liste.js", "recette-fiche.js", "ajouter.js", "enrichir.js", "historique.js")


async def _noop(*a, **k):
    return {}


def _mock_recette(monkeypatch, cached=None):
    async def _get(pid):
        return _recipe("Curry", id="abc") if pid == "abc" else None

    async def _instr(pid):
        return ["Cuire."]
    monkeypatch.setattr(main.notion, "get_recipe", _get)
    monkeypatch.setattr(main.notion, "get_recipe_instructions", _instr)
    for name in ("update_ingredients", "rewrite_recipe_body", "update_image", "update_portions",
                 "update_recipe_title", "update_recipe_url", "update_recipe_meta"):
        monkeypatch.setattr(main.notion, name, _noop)
    if cached:
        asyncio.run(main.db.save_enriched("abc", "Curry", **cached))


def _cache(nid="abc") -> dict:
    return asyncio.run(main.db.get_enriched(nid)) or {}


# ── Gabarits : plus de JS ni de style en ligne ───────────────────────────


@pytest.mark.parametrize("name", PAGES)
def test_aucun_script_inline_hors_page_data(name):
    src = (TEMPLATES / name).read_text(encoding="utf-8")
    for m in re.finditer(r"<script\b([^>]*)>", src):
        attrs = m.group(1)
        assert "src=" in attrs or 'id="page-data"' in attrs, (name, attrs)
    assert "<style" not in src, name
    assert "fetch(" not in src and "function esc" not in src, name


@pytest.mark.parametrize("name", PAGES)
def test_common_js_charge_avant_les_scripts_de_page(name):
    src = (TEMPLATES / name).read_text(encoding="utf-8")
    scripts = re.findall(r'<script src="/static/js/([^"?]+)\?v=\{\{ asset_version \}\}"', src)
    assert scripts and scripts[0] == "common.js", (name, scripts)


def test_nouveaux_fichiers_servis_et_sans_fetch_direct(client):
    for name in NOUVEAUX_JS:
        r = client.get(f"/static/js/{name}")
        assert r.status_code == 200 and r.text.strip(), name
        # Tout passe par Common.requestJson, sauf le flux d'enrichissement
        # (POST lu en flux, recettes-liste.js) qui n'est pas une réponse JSON.
        flux = r.text.count("fetch('/api/enrich-all/stream', {method: 'POST'") if name == "recettes-liste.js" else 0
        assert r.text.count("fetch(") == flux, name
    assert "Common.modal" in (JS / "historique.js").read_text(encoding="utf-8")
    assert "Common.modal" in (JS / "recette-fiche.js").read_text(encoding="utf-8")
    assert "Common.modal" in (JS / "recette-planifier.js").read_text(encoding="utf-8")
    assert "keydown" not in (JS / "recette-planifier.js").read_text(encoding="utf-8")
    css = client.get("/static/css/historique.css")
    assert css.status_code == 200 and ".hist-preview" in css.text


def test_common_js_idempotent():
    src = (JS / "common.js").read_text(encoding="utf-8")
    assert "if (window.Common) return;" in src


NODE_DOUBLE = r"""
global.window = {};
global.document = {getElementById: () => null};
global.navigator = {};
require(process.argv[1]);
const premier = window.Common;
delete require.cache[require.resolve(process.argv[1])];
require(process.argv[1]);
console.log(JSON.stringify({meme: window.Common === premier, modal: typeof window.Common.modal}));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="Node absent")
def test_common_js_charge_deux_fois_garde_la_premiere_copie():
    out = subprocess.run(["node", "-e", NODE_DOUBLE, str(JS / "common.js")],
                         capture_output=True, text=True, check=True, timeout=30).stdout
    assert json.loads(out) == {"meme": True, "modal": "function"}


# ── Historique ───────────────────────────────────────────────────────────


def test_historique_dates_lisibles_et_liens_du_brouillon(client, monkeypatch):
    poulet = _recipe("Poulet", id="p1")

    async def _all():
        return [poulet]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    r = client.post("/construire", data={
        "week_start": "2026-01-05", "action": "brouillon", "draft_id": "",
        "meals": json.dumps([_case(1, "midi", poulet, persons=2)]),
    }, follow_redirects=False)
    pid = int(re.search(r"draft=(\d+)", r.headers["location"]).group(1))
    html = client.get("/historique").text
    assert '/static/css/historique.css?v=' in html
    assert '/static/js/historique.js?v=' in html
    assert re.search(r"Créé le \d{2}/\d{2}/\d{4}", html)
    assert not re.search(r"Créé le \d{4}-\d{2}-\d{2}", html)
    carte = html[html.index(f'data-card="{pid}"'):]
    carte = carte[:carte.index("</div>\n    </div>")]
    assert f'href="/?draft={pid}"' in carte and "Reprendre" in carte
    assert f'href="/planning/{pid}"' in carte and "Voir" in carte


# ── Fiche : données serveur par #page-data ───────────────────────────────


def test_fiche_page_data_porte_les_portions(client, monkeypatch):
    _mock_recette(monkeypatch, cached={"ingredients": json.dumps([{"nom": "riz", "quantite": "200", "unite": "g"}]),
                                       "base_servings": 3})
    html = client.get("/recette/abc").text
    data = json.loads(html.split('id="page-data">', 1)[1].split("</script>", 1)[0])
    assert data["recette_id"] == "abc" and data["base_servings"] == 3
    assert "/static/js/recette-fiche.js?v=" in html


def test_enrichir_page_data(client, monkeypatch):
    _mock_recette(monkeypatch)
    html = client.get("/recette/abc/enrichir").text
    data = json.loads(html.split('id="page-data">', 1)[1].split("</script>", 1)[0])
    assert data == {"recette_id": "abc"}


# ── Édition : cache local et message d'échec ─────────────────────────────


def test_edition_sans_ingredient_ecrit_portions_et_duree_dans_le_cache(client, monkeypatch):
    _mock_recette(monkeypatch)
    r = client.post("/recette/abc/enrichir", data={
        "ingredients_text": "", "base_servings": "6", "duree_minutes": "40",
    }, follow_redirects=False)
    assert r.status_code == 303
    c = _cache()
    assert c["base_servings"] == 6 and c["cuisson_minutes"] == 40


def test_edition_ingredients_vides_vident_le_cache(client, monkeypatch):
    _mock_recette(monkeypatch, cached={
        "ingredients": json.dumps([{"nom": "riz", "quantite": "200", "unite": "g"}]),
        "base_servings": 2, "cuisson_minutes": 25,
    })
    ecrits = []

    async def _ings(pid, txt):
        ecrits.append(txt)
    monkeypatch.setattr(main.notion, "update_ingredients", _ings)
    client.post("/recette/abc/enrichir", data={"ingredients_text": "  \n", "base_servings": "2",
                                               "duree_minutes": "25"}, follow_redirects=False)
    c = _cache()
    assert c["ingredients"] == "" and c["base_servings"] == 2 and c["cuisson_minutes"] == 25
    assert ecrits == []          # Notion n'est pas vidé
    page = client.get("/recette/abc").text
    assert "Aucun ingrédient enregistré" in page


def test_edition_decoupe_sel_poivre(client, monkeypatch):
    _mock_recette(monkeypatch)
    client.post("/recette/abc/enrichir", data={"ingredients_text": "200 g riz\nsel, poivre"},
                follow_redirects=False)
    noms = [i["nom"].lower() for i in json.loads(_cache()["ingredients"])]
    assert noms == ["riz", "sel", "poivre"]


def test_edition_echec_liste_ce_qui_est_enregistre(client, monkeypatch):
    _mock_recette(monkeypatch)

    async def _ko(*a, **k):
        raise RuntimeError("Notion indisponible")
    monkeypatch.setattr(main.notion, "update_image", _ko)
    r = client.post("/recette/abc/enrichir", data={
        "ingredients_text": "200 g riz", "steps": ["Cuire."], "image_url": "http://img",
    }, follow_redirects=False)
    assert r.status_code == 200
    t = r.text
    assert "Échec d&#39;enregistrement dans Notion pour : image." in t or \
        "Échec d'enregistrement dans Notion pour : image." in t
    assert "Bien enregistré dans Notion : ingrédients, portions, instructions, type/tags/base." in t
    assert "cache local des ingrédients a bien été enregistré" in t
    assert "Rien n&#39;a été validé" not in t and "Rien n'a été validé" not in t


def test_edition_echec_sans_ingredient_mentionne_le_cache_des_portions(client, monkeypatch):
    _mock_recette(monkeypatch)

    async def _ko(*a, **k):
        raise RuntimeError("Notion indisponible")
    monkeypatch.setattr(main.notion, "update_portions", _ko)
    r = client.post("/recette/abc/enrichir", data={"base_servings": "3"}, follow_redirects=False)
    assert "pour : portions." in r.text
    assert "cache local (portions, durée) a bien été enregistré" in r.text
    assert _cache()["base_servings"] == 3


def test_extraction_source_decoupe_sel_poivre(client, monkeypatch):
    _mock_recette(monkeypatch)

    async def _extract(url):
        return {"ingredients": ["1 kg poulet", "sel, poivre"], "instructions": "Cuire."}
    monkeypatch.setattr(main.llm, "extract_recipe_from_url", _extract)
    html = client.get("/recette/abc/enrichir", params={"refetch": 1, "url": "http://source"}).text
    texte = html.split('id="ingredients_text"', 1)[1].split("</textarea>", 1)[0]
    lignes = [x.strip().lower() for x in texte.split(">", 1)[1].splitlines() if x.strip()]
    assert lignes == ["1 kg poulet", "sel", "poivre"]


# ── Ajout : « sel, poivre » découpé dans le cache ────────────────────────


def test_ajout_decoupe_sel_poivre(client, monkeypatch):
    async def _create(**kw):
        return {"id": "newid", "url": ""}
    monkeypatch.setattr(main.notion, "create_recipe", _create)
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)
    r = client.post("/ajouter-recette", data={"nom": "Poulet", "ingredients_manual": "1 kg poulet\nsel et poivre"})
    assert "succès" in r.text
    noms = [i["nom"].lower() for i in json.loads(_cache("newid")["ingredients"])]
    assert noms == ["poulet", "sel", "poivre"]
