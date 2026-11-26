"""L09 — placard du foyer (« Toujours à la maison »), stocké dans app_prefs."""

import asyncio

import app.main as main
from app.services.shopping_list import get_placard, set_placard, shopping_view
from tests.test_courses_partage import data_of, make_planning, share

SEL = {"Poulet": [{"nom": "poulet", "quantite": "200", "unite": "g"},
                  {"nom": "Sel", "quantite": "1", "unite": "pincée"}]}


def test_sel_au_placard_masque_dans_un_nouveau_planning(client, monkeypatch):
    pid = make_planning(client, monkeypatch, SEL)
    d = client.post(f"/api/courses/{pid}/placard", json={"key": "sel|pincee", "present": True}).json()
    assert d["success"] and d["nb"] == 1
    assert "Masqués (1)" in d["html"] and "toujours à la maison" in d["html"]
    assert asyncio.run(main.db.get_app_prefs())["placard"] == ["sel"]

    # Nouveau planning avec du sel (autre unité) : rangé d'office sous « Masqués ».
    pid2 = make_planning(client, monkeypatch, {"Soupe": [{"nom": "sel", "quantite": "", "unite": ""},
                                                         {"nom": "poireau", "quantite": "2", "unite": ""}]})
    html = client.get(f"/planning/{pid2}").text
    assert 'class="shop-i" data-item="sel|"' not in html
    assert 'data-item="sel|"' in html.split('id="shop-masques"', 1)[1]
    assert 'data-act="deplacard"' in html
    # ni sur le lien public, ni dans l'état publié
    url = share(client, pid2)
    assert 'data-item="sel|"' not in client.get(url).text
    # la liste stockée, elle, garde l'article (le placard s'applique à l'affichage)
    assert any(it["nom"] == "sel" for it in data_of(pid2)["liste_courses"])


def test_retirer_du_placard(client, monkeypatch):
    pid = make_planning(client, monkeypatch, SEL)
    client.post(f"/api/courses/{pid}/placard", json={"key": "sel|pincee", "present": True})
    d = client.post(f"/api/courses/{pid}/placard", json={"key": "sel|pincee", "present": False}).json()
    assert d["nb"] == 2 and "Masqués" not in d["html"]
    assert asyncio.run(main.db.get_app_prefs())["placard"] == []


def test_placard_garde_les_autres_reglages(client, monkeypatch):
    pid = make_planning(client, monkeypatch, SEL)
    asyncio.run(main.db.save_app_prefs({"ordre_rayons": ["Surgelés"], "placard": ["Huile d'olive"]}))
    client.post(f"/api/courses/{pid}/placard", json={"key": "sel|pincee"})
    prefs = asyncio.run(main.db.get_app_prefs())
    assert prefs["ordre_rayons"] == ["Surgelés"]
    assert prefs["placard"] == ["huile d olive", "sel"]


def test_placard_erreurs(client, monkeypatch):
    pid = make_planning(client, monkeypatch, SEL)
    assert client.post(f"/api/courses/{pid}/placard", json={"key": "inconnu|"}).status_code == 404
    assert client.post("/api/courses/9999/placard", json={"key": "sel|pincee"}).status_code == 404
    assert asyncio.run(main.db.get_app_prefs()) == {}


def test_get_placard_tolerant(client):
    asyncio.run(main.db.save_app_prefs({"placard": "sel"}))       # pas une liste
    assert asyncio.run(get_placard()) == []
    asyncio.run(main.db.save_app_prefs({"placard": ["Sel", "", "sel", "Œufs"]}))
    assert asyncio.run(get_placard()) == ["oeuf", "sel"]
    asyncio.run(set_placard("  ", True))                          # nom vide : ignoré
    assert asyncio.run(get_placard()) == ["oeuf", "sel"]


def test_un_article_manuel_n_est_jamais_masque_par_le_placard():
    data = {"liste_courses": [{"nom": "sel", "quantite": "", "unite": ""}],
            "courses_extra": [{"nom": "sel", "quantite": "1", "unite": "boite",
                               "key": "extra:sel|boite"}]}
    view = shopping_view(data, ["sel"])
    assert [v["key"] for v in view["articles"]] == ["extra:sel|boite"]
    assert [(v["key"], v["raison"]) for v in view["masques"]] == [("sel|", "placard")]
