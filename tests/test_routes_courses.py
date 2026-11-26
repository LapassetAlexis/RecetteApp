"""Routes de la liste de courses (génération, coches, partage, mise à l'échelle), Notion simulé."""

import asyncio
import json

import app.main as main
from tests.aides import _case, _construire, _make_shopping_planning, _recipe


def test_alternatives_and_shopping(client, monkeypatch):
    poulet, soupe = _recipe("Poulet", id="p1"), _recipe("Soupe", id="s1")
    async def _all():
        return [poulet, soupe, _recipe("Curry", id="c1")]
    # Liste de courses ANCRÉE sur le cache DB (plus de batch LLM à l'aveugle).
    async def _enriched(nid):
        return {"ingredients": json.dumps([{"nom": "riz", "quantite": "100", "unite": "g"}])}
    async def _noop(*a, **k):
        return {}
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)

    # 2 repas lundi à 8 personnes chacun.
    pid = _construire(client, [_case(1, "midi", poulet, persons=8),
                               _case(1, "soir", soupe, persons=8)])

    alts = client.get(f"/api/alternatives/{pid}").json()["alternatives"]
    assert any(a["nom"] == "Curry" for a in alts)

    shop = client.post(f"/generate-shopping/{pid}").json()
    assert shop.get("success")
    riz = next(i for i in shop["liste_courses"] if i["nom"] == "riz")
    # 2 repas lundi (8 pers) × 100 g / base 4 = 400 g, avec recettes source.
    assert riz["quantite"] == "400"
    assert set(riz["recettes"]) == {"Poulet", "Soupe"}


def test_shopping_check_persists_state(client, monkeypatch):
    pid = _make_shopping_planning(client, monkeypatch)
    # coche "riz" -> persisté dans courses_checked
    r = client.post(f"/api/shopping-check/{pid}", json={"item": "riz", "checked": True})
    assert r.json().get("success")
    p = asyncio.run(main.db.get_planning_with_recipes(pid))
    assert "riz" in json.loads(p["data_json"]).get("courses_checked", [])
    # décoche -> retiré
    assert client.post(f"/api/shopping-check/{pid}", json={"item": "riz", "checked": False}).json().get("success")
    p = asyncio.run(main.db.get_planning_with_recipes(pid))
    assert "riz" not in json.loads(p["data_json"]).get("courses_checked", [])
    # planning introuvable
    assert "error" in client.post("/api/shopping-check/999999", json={"item": "x", "checked": True}).json()


def test_partager_et_courses_public(client, monkeypatch):
    pid = _make_shopping_planning(client, monkeypatch)
    # partager -> URL /courses/<token>
    d = client.post(f"/planning/{pid}/partager").json()
    assert d.get("url", "").startswith("/courses/")
    # idempotent : même token au 2e appel
    assert client.post(f"/planning/{pid}/partager").json()["url"] == d["url"]

    # page publique : 200 et affiche le riz (non coché)
    page = client.get(d["url"])
    assert page.status_code == 200
    assert "riz" in page.text.lower()
    # bouton d'export texte présent + son handler
    assert 'id="pub-copy"' in page.text and "/static/js/courses-public.js" in page.text

    # une fois coché, l'item passe sous « Déjà pris » (L09 : coche partagée)
    client.post(f"/api/shopping-check/{pid}", json={"item": "riz", "checked": True})
    page2 = client.get(d["url"])
    assert page2.status_code == 200
    assert "Riz" in page2.text.split('id="pub-pris"', 1)[1]

    # token inconnu -> 404
    assert client.get("/courses/inexistant").status_code == 404


def test_scaling_par_base_servings_de_la_recette(client, monkeypatch):
    """Le diviseur de mise à l'échelle est la base de portions PROPRE à chaque
    recette (colonne cache), pas la constante globale.

    - « Solo » : base 1, cache 100 g. Repas 2 pers → 2/1 = ×2 → 200 g.
    - « Famille » : base 4, cache 100 g. Repas 2 pers → 2/4 = ×0.5 → 50 g."""

    solo = _recipe("Solo", id="solo", base=["Féculent"])
    famille = _recipe("Famille", id="fam", base=["Féculent"])

    async def _all():
        return [solo, famille]

    caches = {
        "solo": {"ingredients": json.dumps([{"nom": "riz", "quantite": "100", "unite": "g"}]),
                 "base_servings": 1},
        "fam": {"ingredients": json.dumps([{"nom": "pates", "quantite": "100", "unite": "g"}]),
                "base_servings": 4},
    }

    async def _enriched(nid):
        return caches.get(nid)
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)

    pid = _construire(client, [_case(1, "midi", solo, persons=2),
                               _case(1, "soir", famille, persons=2)])
    shop = {i["nom"]: i for i in json.loads(
        asyncio.run(main.db.get_planning_with_recipes(pid))["data_json"])["liste_courses"]}
    assert shop["riz"]["quantite"] == "200"   # base 1 → ×2
    assert shop["pates"]["quantite"] == "50"  # base 4 → ×0.5


def test_scaling_defaut_4_si_base_absente(client, monkeypatch):
    """Recette dont le cache n'a pas de base_servings → défaut 4 (comportement
    identique à l'ancien BASE_SERVINGS global)."""

    plat = _recipe("Plat", id="pl", base=["Féculent"])

    async def _all():
        return [plat]

    async def _enriched(nid):
        # Cache SANS base_servings (ligne « ancienne »).
        return {"ingredients": json.dumps([{"nom": "riz", "quantite": "100", "unite": "g"}])}
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)

    pid = _construire(client, [_case(1, "midi", plat, persons=4)])
    shop = {i["nom"]: i for i in json.loads(
        asyncio.run(main.db.get_planning_with_recipes(pid))["data_json"])["liste_courses"]}
    assert shop["riz"]["quantite"] == "100"  # 4/4 = ×1
