"""L11 — édition directe d'une recette, formulaire d'ajout conservé en cas
d'erreur, Moment / État modifiables, retrait des tags, usage des recettes."""

import json
from datetime import date

import pytest

import app.main as main
from app.services import recettes as recettes_svc
from tests.test_routes import _recipe, client  # noqa: F401  (fixture réutilisée)

CACHE = {
    "ingredients": json.dumps([{"nom": "riz", "quantite": "200", "unite": "g"}]),
    "nutrition": json.dumps({"calories": 500, "proteines": 10, "glucides": 80, "lipides": 5}),
    "cuisson_minutes": 25,
    "base_servings": 2,
}


async def _noop(*a, **k):
    return {}


def _mock_recette(monkeypatch, cached=CACHE, url="http://source", instr=("Corrigé à la main.",), **kw):
    async def _get(pid):
        return _recipe("Curry", id="abc", url=url, **kw) if pid == "abc" else None

    async def _enriched(nid):
        return dict(cached) if cached else None

    async def _instr(pid):
        return list(instr)
    monkeypatch.setattr(main.notion, "get_recipe", _get)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)
    monkeypatch.setattr(main.notion, "get_recipe_instructions", _instr)


def _extract_interdit(monkeypatch):
    async def _boom(url):
        raise AssertionError("extract_recipe_from_url ne doit pas être appelé")
    monkeypatch.setattr(main.llm, "extract_recipe_from_url", _boom)


def _extract_compte(monkeypatch):
    calls = []

    async def _extract(url):
        calls.append(url)
        return {"ingredients": ["300 g pâtes"], "instructions": "Cuire les pâtes.",
                "image_url": "http://img", "duree_minutes": 12}
    monkeypatch.setattr(main.llm, "extract_recipe_from_url", _extract)
    return calls


def _mock_ecritures(monkeypatch, meta: dict | None = None):
    async def _meta(page_id, **kw):
        if meta is not None:
            meta.update(kw)
        return {}

    async def _save(*a, **k):
        return None
    for name in ("update_ingredients", "rewrite_recipe_body", "update_image", "update_portions",
                 "update_recipe_title", "update_recipe_url"):
        monkeypatch.setattr(main.notion, name, _noop)
    monkeypatch.setattr(main.notion, "update_recipe_meta", _meta)
    monkeypatch.setattr(main.db, "save_enriched", _save)


# ── « Modifier » : données enregistrées, sans appel à la source ──────────


def test_modifier_ne_rappelle_pas_la_source(client, monkeypatch):  # noqa: F811
    _mock_recette(monkeypatch)
    _extract_interdit(monkeypatch)
    r = client.get("/recette/abc/enrichir")
    assert r.status_code == 200
    assert "200 g riz" in r.text                      # ingrédients du cache
    assert "Corrigé à la main." in r.text             # étapes Notion
    assert 'name="duree_minutes" value="25"' in r.text  # durée du cache conservée
    assert "&#34;calories&#34;: 500" in r.text or '"calories": 500' in r.text
    assert 'value="2"' in r.text                       # portions du cache
    assert "Ré-extraire depuis la source" in r.text
    assert "remplace les champs ci-dessous" in r.text


def test_refetch_extrait_depuis_la_source(client, monkeypatch):  # noqa: F811
    _mock_recette(monkeypatch)
    calls = _extract_compte(monkeypatch)
    r = client.get("/recette/abc/enrichir?refetch=1")
    assert r.status_code == 200
    assert calls == ["http://source"]
    assert "300 g pâtes" in r.text and "Cuire les pâtes." in r.text
    assert "pré-remplis depuis la source" in r.text


def test_url_fournie_extrait(client, monkeypatch):  # noqa: F811
    _mock_recette(monkeypatch, url="")
    calls = _extract_compte(monkeypatch)
    r = client.get("/recette/abc/enrichir?url=http://autre")
    assert r.status_code == 200 and calls == ["http://autre"]


def test_sans_cache_avec_url_extraction_automatique(client, monkeypatch):  # noqa: F811
    _mock_recette(monkeypatch, cached=None)
    calls = _extract_compte(monkeypatch)
    r = client.get("/recette/abc/enrichir")
    assert calls == ["http://source"] and "300 g pâtes" in r.text


def test_sans_cache_extraction_en_echec(client, monkeypatch):  # noqa: F811
    _mock_recette(monkeypatch, cached=None)

    async def _ko(url):
        raise RuntimeError("source injoignable")
    monkeypatch.setattr(main.llm, "extract_recipe_from_url", _ko)
    r = client.get("/recette/abc/enrichir")
    assert r.status_code == 200 and "Corrigé à la main." in r.text


def test_enrichir_recette_inconnue(client, monkeypatch):  # noqa: F811
    _mock_recette(monkeypatch)
    assert client.get("/recette/zzz/enrichir").status_code == 404
    r = client.post("/recette/zzz/enrichir", data={}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/recettes"


def test_formulaire_moment_et_etat(client, monkeypatch):  # noqa: F811
    _mock_recette(monkeypatch, moment="Soir", etat="Testée")
    _extract_interdit(monkeypatch)
    r = client.get("/recette/abc/enrichir")
    assert 'name="moment"' in r.text and 'name="etat"' in r.text
    assert '<option value="Soir" selected>' in r.text
    assert '<option value="Testée" selected>' in r.text


# ── Enregistrement : Moment, État, retrait des tags ──────────────────────


def test_submit_ecrit_moment_etat_et_vide_les_tags(client, monkeypatch):  # noqa: F811
    _mock_recette(monkeypatch, tags=["Rapide"])
    meta: dict = {}
    _mock_ecritures(monkeypatch, meta)
    r = client.post("/recette/abc/enrichir", data={
        "repas": "Plat", "moment": "Midi", "etat": "Réussie", "ingredients_text": "200 g riz",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert meta["tags"] == []                 # aucune case cochée → tags vidés
    assert meta["moment"] == "Midi" and meta["etat"] == "Réussie"


def test_submit_sans_moment_ni_etat_ne_les_ecrit_pas(client, monkeypatch):  # noqa: F811
    _mock_recette(monkeypatch)
    meta: dict = {}
    _mock_ecritures(monkeypatch, meta)
    client.post("/recette/abc/enrichir", data={"repas": "Plat"}, follow_redirects=False)
    assert "moment" not in meta and "etat" not in meta


def test_submit_conserve_nutrition_et_duree_du_cache(client, monkeypatch):  # noqa: F811
    _mock_recette(monkeypatch)
    _mock_ecritures(monkeypatch)
    saved = {}

    async def _save(nid, name, **k):
        saved.update(k)
    monkeypatch.setattr(main.db, "save_enriched", _save)
    page = client.get("/recette/abc/enrichir").text
    assert 'name="duree_minutes" value="25"' in page
    client.post("/recette/abc/enrichir", data={
        "ingredients_text": "200 g riz", "duree_minutes": "25", "nutrition_json": CACHE["nutrition"],
    }, follow_redirects=False)
    assert saved["cuisson_minutes"] == 25 and json.loads(saved["nutrition"])["calories"] == 500


def test_submit_echec_notion_reaffiche_la_saisie(client, monkeypatch):  # noqa: F811
    _mock_recette(monkeypatch)
    _mock_ecritures(monkeypatch)

    async def _ko(*a, **k):
        raise RuntimeError("Notion indisponible")
    monkeypatch.setattr(main.notion, "update_recipe_meta", _ko)
    r = client.post("/recette/abc/enrichir", data={
        "nom": "Curry vert", "tags": ["Rapide"], "moment": "Soir", "etat": "Testée",
        "ingredients_text": "200 g riz", "steps": ["Cuire."],
    }, follow_redirects=False)
    assert r.status_code == 200
    assert "type/tags/base" in r.text and "cache local des ingrédients a bien" in r.text
    assert 'value="Curry vert"' in r.text and "Cuire." in r.text
    assert '<option value="Soir" selected>' in r.text


# ── Ajout : la saisie survit à une erreur ────────────────────────────────


def _mock_create(monkeypatch, exc: Exception | None = None):
    async def _create(**kw):
        if exc:
            raise exc
        return {"id": "newid", "url": "https://notion/newid"}
    monkeypatch.setattr(main.notion, "create_recipe", _create)


def test_ajout_erreur_notion_conserve_la_saisie(client, monkeypatch):  # noqa: F811
    _mock_create(monkeypatch, RuntimeError("Notion indisponible"))
    r = client.post("/ajouter-recette", data={
        "nom": "Gratin", "repas": "Plat", "tags": ["Rapide"], "base": ["Légume"],
        "moment": "Soir", "nature": "Recette", "base_servings": "6",
        "ingredients_manual": "200 g pommes de terre\ncrème", "steps": ["Éplucher.", "Cuire."],
        "image_url": "http://img",
    })
    assert r.status_code == 200 and "Notion indisponible" in r.text
    t = r.text
    assert 'value="Gratin"' in t
    assert "200 g pommes de terre\ncrème</textarea>" in t
    assert "Éplucher.</textarea>" in t and "Cuire.</textarea>" in t
    assert 'value="Rapide" class="tag-cb" checked' in t
    assert 'value="Légume" class="base-cb" checked' in t
    assert 'value="Plat" class="repas-cb" checked' in t
    assert '<option value="Soir" selected>' in t
    assert 'value="6"' in t and 'value="http://img"' in t
    assert 'id="step1" style="display:none;"' in t       # directement à l'étape 2
    assert 'id="step2" style="display:none;"' not in t


def test_ajout_sans_nom_conserve_la_saisie(client, monkeypatch):  # noqa: F811
    r = client.post("/ajouter-recette", data={"ingredients_manual": "1 oeuf"})
    assert "Il faut au moins un nom" in r.text and "1 oeuf</textarea>" in r.text


def test_ajout_echec_partiel_ne_repropose_pas_le_formulaire(client, monkeypatch):  # noqa: F811
    _mock_create(monkeypatch)

    async def _ko(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(main.notion, "update_ingredients", _ko)
    monkeypatch.setattr(main.notion, "append_instructions", _noop)
    r = client.post("/ajouter-recette", data={"nom": "Gratin", "ingredients_manual": "1 oeuf",
                                              "steps": ["Cuire."]})
    assert "créée dans Notion, mais impossible d" in r.text and "enregistrer : les ingrédients" in r.text
    assert 'href="/recette/newid/enrichir"' in r.text
    assert 'id="step2" style="display:none;"' in r.text   # pas de doublon possible


def test_ajout_depuis_url_extrait_le_nom(client, monkeypatch):  # noqa: F811
    created = {}

    async def _create(**kw):
        created.update(kw)
        return {"id": "newid"}

    async def _extract(url):
        return {"nom": "Tarte", "type_repas": "Dessert", "tags": ["Fun"], "base": []}
    monkeypatch.setattr(main.notion, "create_recipe", _create)
    monkeypatch.setattr(main.llm, "extract_recipe_from_url", _extract)
    r = client.post("/ajouter-recette", data={"url": "http://tarte"})
    assert "succès" in r.text and 'href="/recette/newid"' in r.text
    assert created["nom"] == "Tarte" and created["repas"] == ["Dessert"] and created["tags"] == ["Fun"]


def test_page_ajouter_vide(client):  # noqa: F811
    r = client.get("/ajouter")
    assert r.status_code == 200 and 'id="step2" style="display:none;"' in r.text


# ── Usage : fiche et liste ───────────────────────────────────────────────


@pytest.mark.parametrize("usage,attendu", [
    (None, ""),
    ({"count": 0, "last": ""}, ""),
    ({"count": 2, "last": "bad"}, "Cuisinée 2 fois"),
    ({"count": 3, "last": f"{date.today().year}-10-12"}, "Cuisinée 3 fois · dernière fois le 12/10"),
    ({"count": 1, "last": "2020-01-05"}, "Cuisinée 1 fois · dernière fois le 05/01/2020"),
])
def test_usage_label(usage, attendu):
    assert recettes_svc._usage_label(usage) == attendu


def test_fiche_affiche_usage_et_bouton_planning(client, monkeypatch):  # noqa: F811
    _mock_recette(monkeypatch)

    async def _usage():
        return {"abc": {"count": 3, "last": f"{date.today().year}-10-12"}}
    monkeypatch.setattr(main.db, "get_recipe_usage", _usage)
    r = client.get("/recette/abc")
    assert "Cuisinée 3 fois · dernière fois le 12/10" in r.text
    assert 'id="btn-planifier"' in r.text and "/static/js/recette-planifier.js" in r.text
    assert "✏️ Modifier" in r.text


def test_liste_tri_et_usage(client, monkeypatch):  # noqa: F811
    async def _all():
        return [_recipe("Tarte", "Dessert", id="t1"), _recipe("Curry", id="c1")]

    async def _usage():
        return {"c1": {"count": 2, "last": "2020-03-01"}}
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.db, "get_recipe_usage", _usage)
    r = client.get("/recettes")
    assert r.status_code == 200
    assert 'id="recipe-sort"' in r.text and "Pas faite depuis longtemps" in r.text and "Mieux notées" in r.text
    assert 'data-last="2020-03-01" data-count="2"' in r.text
    assert "Cuisinée 2 fois · dernière fois le 01/03/2020" in r.text
    assert "onclick=" not in r.text


def test_usage_illisible_ne_casse_pas_la_liste(client, monkeypatch):  # noqa: F811
    async def _all():
        return [_recipe("Tarte", id="t1")]

    async def _ko():
        raise RuntimeError("db")
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.db, "get_recipe_usage", _ko)
    assert client.get("/recettes").status_code == 200
