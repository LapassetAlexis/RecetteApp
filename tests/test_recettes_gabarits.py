"""Gabarits des pages Recettes : accessibilité clavier et code JS mutualisé (L06)."""

import json
import re
from pathlib import Path

import app.main as main
from tests.aides import _recipe

TEMPLATES = Path(main.BASE_DIR) / "templates"
RATING_SRC = '<script src="/static/js/rating.js'
STEPS_SRC = '<script src="/static/js/steps-editor.js'


def _mock_liste(monkeypatch):
    async def _all():
        r = _recipe("Tarte", "Dessert", id="t1", tags=["Fun"])
        r["note"] = "⭐⭐⭐"
        return [r, _recipe("Curry", id="c1")]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)


def _mock_fiche(monkeypatch, note=""):
    async def _get(pid):
        if pid != "abc":
            return None
        r = _recipe("Tarte", "Dessert", id="abc")
        r["note"] = note
        return r

    async def _instr(pid):
        return ["Préchauffer le four.", "Enfourner."]

    async def _enriched(nid):
        return {"ingredients": json.dumps([{"nom": "farine", "quantite": "200", "unite": "g"}])}
    monkeypatch.setattr(main.notion, "get_recipe", _get)
    monkeypatch.setattr(main.notion, "get_recipe_instructions", _instr)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)


def test_assets_partages_servis(client):
    for path, marker in (
        ("/static/js/rating.js", "/api/rate/"),
        ("/static/js/steps-editor.js", "addStepRow"),
        ("/static/css/recettes.css", ".recipe-card-link"),
    ):
        r = client.get(path)
        assert r.status_code == 200, path
        assert marker in r.text, path


def test_liste_cartes_en_liens_et_etoiles_boutons(client, monkeypatch):
    _mock_liste(monkeypatch)
    html = client.get("/recettes").text
    assert RATING_SRC in html
    assert "/static/css/recettes.css" in html
    # le titre de chaque carte est un lien vers la fiche, plus de data-href
    assert '<a class="recipe-card-link" href="/recette/t1">Tarte</a>' in html
    assert '<a class="recipe-card-link" href="/recette/c1">Curry</a>' in html
    assert "data-href" not in html
    # étoiles : 5 boutons par carte, nommés et à l'état pressé cohérent
    assert html.count('aria-label="Noter 3 sur 5"') == 2
    assert '<span class="star' not in html
    tarte = html[html.index('href="/recette/t1"'):html.index('href="/recette/c1"')]
    assert tarte.count('aria-pressed="true"') == 1
    assert 'aria-label="Noter 3 sur 5" aria-pressed="true"' in tarte
    curry = html[html.index('href="/recette/c1"'):]
    assert re.search(r'class="star-clear"[^>]*\bhidden\b', curry)    # pas de note → masqué


def test_liste_chips_de_filtre_en_boutons(client, monkeypatch):
    _mock_liste(monkeypatch)
    html = client.get("/recettes").text
    assert '<span class="stat-chip' not in html
    chips = re.findall(r'<button type="button"[^>]*class="stat-chip[^"]*"[^>]*>', html)
    assert chips and all("aria-pressed=" in c for c in chips)
    assert re.search(r'aria-pressed="true" class="stat-chip active" data-filter="nature" data-value="Recette"', html)
    assert 'id="chip-rapide" aria-pressed="false"' in html
    js = client.get("/static/js/recettes-liste.js").text          # JS sorti en fichier (M08)
    assert "setTimeout(applyFilters, 120)" in js                     # recherche temporisée


def test_fiche_notation_partagee_et_modale_accessible(client, monkeypatch):
    _mock_fiche(monkeypatch, note="⭐⭐")
    html = client.get("/recette/abc").text
    assert RATING_SRC in html
    assert html.count('class="star ') == 5
    assert 'aria-label="Noter 2 sur 5" aria-pressed="true"' in html
    assert re.search(r'class="star-clear"[^>]*aria-label="Retirer la note">', html)  # visible
    assert 'role="dialog"' in html and 'aria-modal="true"' in html
    assert 'aria-labelledby="del-title"' in html and 'id="del-title"' in html
    assert "'Escape'" in html + client.get("/static/js/common.js").text  # Common.modal (M08)


def test_aucun_bloc_de_notation_inline():
    for name in ("recettes.html", "recette_detail.html"):
        src = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "/api/rate/" not in src, name
        assert RATING_SRC in src, name


def test_editeur_etapes_mutualise(client, monkeypatch):
    for name in ("ajouter.html", "enrichir.html"):
        src = (TEMPLATES / name).read_text(encoding="utf-8")
        assert STEPS_SRC in src, name
        assert "function renumberSteps" not in src, name
        assert "function bindDel" not in src, name
        assert "function addStepRow" not in src, name

    html = client.get("/ajouter").text
    assert STEPS_SRC in html
    assert 'placeholder="Un ingrédient par ligne : 200 g de farine"' in html

    async def _get(pid):
        return _recipe("Curry", id="abc", url="")

    async def _enriched(nid):
        return None

    async def _instr(pid):
        return ["Cuire le riz."]
    monkeypatch.setattr(main.notion, "get_recipe", _get)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)
    monkeypatch.setattr(main.notion, "get_recipe_instructions", _instr)
    page = client.get("/recette/abc/enrichir")
    assert page.status_code == 200
    assert STEPS_SRC in page.text and "Cuire le riz." in page.text
