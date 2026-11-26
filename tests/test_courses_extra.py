"""L09 — articles ajoutés à la main et articles masqués (« Je l'ai déjà »)."""

import json
from urllib.parse import quote

from app.services.shopping_list import add_extra, make_extra, remove_extra, set_masque
from tests.aides import _case, _recipe
from tests.test_courses_partage import data_of, make_planning, set_data, share

RIZ = {"Poulet": [{"nom": "riz", "quantite": "100", "unite": "g"},
                  {"nom": "beurre", "quantite": "20", "unite": "g"}]}


# ── Fonctions de service ──────────────────────────────────────────────

def test_make_extra_passe_par_parse_ingredient_line():
    it = make_extra("2 l lait")
    assert it == {"nom": "lait", "quantite": "2", "unite": "l",
                  "rayon": "Crémerie & œufs", "key": "extra:lait|volume"}
    assert make_extra("papier toilette")["key"] == "extra:papier toilette|"
    assert make_extra("   ") is None
    assert make_extra("") is None


def test_add_extra_additionne_le_meme_article():
    data = {}
    add_extra(data, "2 l lait")
    add_extra(data, "50 cl lait")
    assert len(data["courses_extra"]) == 1
    assert (data["courses_extra"][0]["quantite"], data["courses_extra"][0]["unite"]) == ("2.5", "l")


def test_remove_extra_retire_aussi_la_coche():
    data = {"courses_checked": ["extra:cafe|"]}
    add_extra(data, "café")
    assert remove_extra(data, "extra:cafe|")
    assert data["courses_extra"] == [] and data["courses_checked"] == []
    assert not remove_extra(data, "extra:cafe|")


def test_set_masque_refuse_un_article_manuel():
    data = {"liste_courses": [{"nom": "riz", "quantite": "1", "unite": "kg"}]}
    add_extra(data, "café")
    assert not set_masque(data, "extra:cafe|", True)
    assert set_masque(data, "riz|masse", True) and data["courses_masques"] == ["riz|masse"]
    assert set_masque(data, "riz|masse", False) and data["courses_masques"] == []


# ── Routes : articles manuels ─────────────────────────────────────────

def test_ajout_article_manuel_renvoie_la_liste_rendue(client, monkeypatch):
    pid = make_planning(client, monkeypatch, RIZ)
    r = client.post(f"/api/courses/{pid}/extra", json={"texte": "2 l lait"})
    d = r.json()
    assert r.status_code == 200 and d["success"]
    assert d["item"]["key"] == "extra:lait|volume" and d["nb"] == 3
    assert 'data-item="extra:lait|volume"' in d["html"] and "<small>2 l</small>" in d["html"]
    # visible sur la page planning (ligne en pointillés) et sur la page publique
    assert 'class="shop-i shop-extra" data-item="extra:lait|volume"' in client.get(f"/planning/{pid}").text
    assert 'data-item="extra:lait|volume"' in client.get(share(client, pid)).text


def test_article_manuel_survit_a_la_regeneration(client, monkeypatch):
    pid = make_planning(client, monkeypatch, RIZ)
    client.post(f"/api/courses/{pid}/extra", json={"texte": "papier toilette"})
    client.post(f"/api/shopping-check/{pid}", json={"item": "extra:papier toilette|", "checked": True})
    client.post(f"/api/shopping-check/{pid}", json={"item": "riz|masse", "checked": True})
    assert client.post(f"/generate-shopping/{pid}").json()["success"]
    data = data_of(pid)
    assert [e["nom"] for e in data["courses_extra"]] == ["papier toilette"]
    # la coche de l'article manuel est conservée, celles des recettes repartent de zéro
    assert data["courses_checked"] == ["extra:papier toilette|"]
    assert "Papier toilette" in client.get(f"/planning/{pid}").text


def test_article_manuel_survit_a_un_changement_de_repas(client, monkeypatch):
    pid = make_planning(client, monkeypatch, {**RIZ, "Curry": [{"nom": "curry", "quantite": "1", "unite": "pot"}]})
    client.post(f"/api/courses/{pid}/extra", json={"texte": "café"})
    client.post(f"/api/shopping-check/{pid}", json={"item": "extra:cafe|", "checked": True})
    client.post(f"/api/planning/{pid}/meals",
                json={"slots": [{"jour": 1, "moment": "midi"}], "action": "set_main", "nom": "Curry"})
    data = data_of(pid)
    assert [e["key"] for e in data["courses_extra"]] == ["extra:cafe|"]
    assert data["courses_checked"] == ["extra:cafe|"]


def test_ajout_sur_une_liste_pas_encore_generee(client, monkeypatch):
    pid = make_planning(client, monkeypatch, RIZ, generate=False)
    data = data_of(pid)
    data["liste_courses"] = []
    set_data(pid, data)
    d = client.post(f"/api/courses/{pid}/extra", json={"texte": "café"}).json()
    assert d["success"] and "Café" in d["html"] and 'id="btn-shop"' in d["html"]


def test_retrait_article_manuel(client, monkeypatch):
    pid = make_planning(client, monkeypatch, RIZ)
    client.post(f"/api/courses/{pid}/extra", json={"texte": "café"})
    key = quote("extra:cafe|", safe="")
    d = client.delete(f"/api/courses/{pid}/extra/{key}").json()
    assert d["success"] and "extra:cafe" not in d["html"]
    assert data_of(pid)["courses_extra"] == []
    assert client.delete(f"/api/courses/{pid}/extra/{key}").status_code == 404


def test_erreurs_articles_manuels(client, monkeypatch):
    pid = make_planning(client, monkeypatch, RIZ)
    assert client.post(f"/api/courses/{pid}/extra", json={"texte": "  "}).status_code == 400
    assert client.post(f"/api/courses/{pid}/extra", content=b"x").status_code == 400
    assert client.post("/api/courses/9999/extra", json={"texte": "café"}).status_code == 404
    assert client.delete("/api/courses/9999/extra/x").status_code == 404


# ── Routes : masquer / restaurer ─────────────────────────────────────

def test_masquer_puis_restaurer(client, monkeypatch):
    pid = make_planning(client, monkeypatch, RIZ)
    url = share(client, pid)
    d = client.post(f"/api/courses/{pid}/masquer", json={"key": "riz|masse", "masque": True}).json()
    assert d["success"] and d["nb"] == 1
    assert "Masqués (1)" in d["html"] and 'data-act="restaurer"' in d["html"]
    assert 'class="shop-i" data-item="riz|masse"' not in d["html"]
    assert 'data-item="riz|masse"' not in client.get(url).text      # absent du lien public
    assert client.get(f"{url}/etat").json()["checked"] == []

    d = client.post(f"/api/courses/{pid}/masquer", json={"key": "riz|masse", "masque": False}).json()
    assert d["nb"] == 2 and "Masqués" not in d["html"]
    assert data_of(pid)["courses_masques"] == []


def test_masquer_erreurs(client, monkeypatch):
    pid = make_planning(client, monkeypatch, RIZ)
    assert client.post(f"/api/courses/{pid}/masquer", json={"key": "inconnu|"}).status_code == 404
    assert client.post("/api/courses/9999/masquer", json={"key": "riz|masse"}).status_code == 404


def test_coches_survivent_a_la_resoumission_du_constructeur(client, monkeypatch):
    """/construire sur un planning existant (draft_id) : articles manuels et leurs
    coches conservés, coche par clé d'un article de recette encore présent aussi."""
    pid = make_planning(client, monkeypatch, RIZ)
    client.post(f"/api/courses/{pid}/extra", json={"texte": "café"})
    for key in ("extra:cafe|", "riz|masse"):
        client.post(f"/api/shopping-check/{pid}", json={"item": key, "checked": True})
    poulet = _recipe("Poulet", id="r0")
    r = client.post("/construire", data={
        "week_start": "2026-01-05", "draft_id": str(pid),
        "meals": json.dumps([_case(1, "midi", poulet, persons=4)]),
    }, follow_redirects=False)
    assert r.headers["location"].endswith(f"/planning/{pid}")
    data = data_of(pid)
    assert [e["key"] for e in data["courses_extra"]] == ["extra:cafe|"]
    assert data["courses_checked"] == ["extra:cafe|", "riz|masse"]


def test_masque_survit_a_la_regeneration(client, monkeypatch):
    pid = make_planning(client, monkeypatch, RIZ)
    client.post(f"/api/courses/{pid}/masquer", json={"key": "beurre|masse"})
    client.post(f"/generate-shopping/{pid}")
    assert data_of(pid)["courses_masques"] == ["beurre|masse"]
