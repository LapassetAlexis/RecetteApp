"""M06 — gestion du placard, rayon et saisie des articles manuels,
rafraîchissement des coches de la page planning."""

import asyncio
from urllib.parse import quote

import pytest

import app.main as main
from app.categories import RAYON_ORDER
from app.services.shopping_list import add_extra, get_placard, make_extra, set_extra_rayon, set_placard
from tests.test_courses_partage import data_of, make_planning, set_data, share

SEL = {"Poulet": [{"nom": "poulet", "quantite": "200", "unite": "g"},
                  {"nom": "Sel", "quantite": "1", "unite": "pincée"}]}


# ── Placard : écran de gestion ────────────────────────────────────────

def test_placard_vide_par_defaut(client):
    assert client.get("/api/placard").json() == {"placard": []}


def test_retrait_du_placard_sans_ouvrir_le_planning(client, monkeypatch):
    """Critère : un article mis au placard depuis un planning se retire depuis
    l'écran du placard, sans passer par ce planning."""
    pid = make_planning(client, monkeypatch, SEL)
    client.post(f"/api/courses/{pid}/placard", json={"key": "sel|pincee", "present": True})
    asyncio.run(set_placard("Huile d'olive", True))
    # Le placard garde les noms normalisés (minuscules, sans accent ni apostrophe).
    assert client.get("/api/placard").json() == {"placard": ["huile d olive", "sel"]}

    r = client.delete("/api/placard/sel")
    assert r.status_code == 200
    assert r.json() == {"success": True, "placard": ["huile d olive"]}
    assert asyncio.run(get_placard()) == ["huile d olive"]
    # L'article revient dans la liste du planning, sans autre action.
    html = client.get(f"/planning/{pid}").text
    assert 'class="shop-i" data-item="sel|pincee"' in html
    assert "Placard (<span id=\"placard-n\">1</span>)" in html


def test_retrait_avec_planning_renvoie_la_liste(client, monkeypatch):
    pid = make_planning(client, monkeypatch, SEL)
    client.post(f"/api/courses/{pid}/placard", json={"key": "sel|pincee", "present": True})
    d = client.delete(f"/api/placard/{quote('Sel')}?planning={pid}").json()
    assert d["success"] and d["placard"] == [] and d["nb"] == 2 and d["nb_placard"] == 0
    assert 'data-item="sel|pincee"' in d["html"] and "Masqués" not in d["html"]


def test_retrait_d_un_nom_absent_404(client):
    r = client.delete("/api/placard/inconnu")
    assert r.status_code == 404 and r.json()["error"]


def test_retrait_garde_les_autres_reglages(client):
    asyncio.run(main.db.save_app_prefs({"autre": 1, "placard": ["sel", "poivre"]}))
    client.delete("/api/placard/poivre")
    assert asyncio.run(main.db.get_app_prefs()) == {"autre": 1, "placard": ["sel"]}


def test_set_placard_indique_un_changement(client):
    async def _go():
        return [await set_placard("sel", True), await set_placard("sel", True),
                await set_placard("sel", False), await set_placard("sel", False)]
    assert asyncio.run(_go()) == [True, False, True, False]


def test_compteur_du_placard_dans_les_reponses_d_action(client, monkeypatch):
    pid = make_planning(client, monkeypatch, SEL)
    d = client.post(f"/api/courses/{pid}/placard", json={"key": "sel|pincee", "present": True}).json()
    assert d["nb_placard"] == 1


# ── Article manuel : parenthèses, nom numérique, rayon ─────────────────

def test_parentheses_gardees():
    it = make_extra("Papier toilette (x6)")
    assert it["nom"] == "Papier toilette (x6)"
    assert it["key"] == "extra:papier toilette x6|"


@pytest.mark.parametrize("texte", ["2", "3,5", "  42 ", "1/2"])
def test_nom_purement_numerique_refuse(texte):
    assert make_extra(texte) is None


def test_nom_numerique_refuse_par_la_route(client, monkeypatch):
    pid = make_planning(client, monkeypatch, SEL)
    r = client.post(f"/api/courses/{pid}/extra", json={"texte": "2"})
    assert r.status_code == 400
    assert not data_of(pid).get("courses_extra")


def test_rayon_d_un_article_manuel():
    data = {}
    add_extra(data, "pile AA")
    key = data["courses_extra"][0]["key"]
    assert set_extra_rayon(data, key, "Boissons")
    assert data["courses_extra"][0]["rayon"] == "Boissons"
    assert not set_extra_rayon(data, key, "Rayon imaginaire")
    assert not set_extra_rayon(data, "extra:inconnu|", "Boissons")
    # Même article ressaisi : la quantité s'additionne, le rayon choisi reste.
    add_extra(data, "pile AA")
    assert data["courses_extra"][0]["rayon"] == "Boissons"


def test_route_rayon(client, monkeypatch):
    pid = make_planning(client, monkeypatch, SEL)
    key = client.post(f"/api/courses/{pid}/extra", json={"texte": "lessive"}).json()["item"]["key"]
    r = client.post(f"/api/courses/{pid}/extra/{quote(key)}/rayon", json={"rayon": "Surgelés"})
    d = r.json()
    assert r.status_code == 200 and d["success"]
    bloc = d["html"].split('data-rayon="Surgelés"', 1)[1].split('class="shop-rayon"', 1)[0]
    assert f'data-item="{key}"' in bloc
    assert data_of(pid)["courses_extra"][0]["rayon"] == "Surgelés"
    # Rayon inconnu, article de recette, planning absent.
    assert client.post(f"/api/courses/{pid}/extra/{quote(key)}/rayon", json={"rayon": "X"}).status_code == 400
    assert client.post(f"/api/courses/{pid}/extra/poulet%7Cmasse/rayon",
                       json={"rayon": "Surgelés"}).status_code == 400
    assert client.post(f"/api/courses/999/extra/{quote(key)}/rayon",
                       json={"rayon": "Surgelés"}).status_code == 404


def test_selecteur_de_rayon_rendu_pour_les_articles_manuels(client, monkeypatch):
    pid = make_planning(client, monkeypatch, SEL)
    html = client.post(f"/api/courses/{pid}/extra", json={"texte": "lessive"}).json()["html"]
    assert html.count('class="shop-act shop-rayon-sel"') == 1      # pas sur les articles de recette
    for rayon in RAYON_ORDER:
        assert f'<option value="{rayon}"' in html.replace("&amp;", "&")


# ── Rafraîchissement de la page planning ───────────────────────────────

def test_etat_page_planning_identique_a_la_route_publique(client, monkeypatch):
    pid = make_planning(client, monkeypatch, SEL)
    url = share(client, pid)
    client.post(f"{url}/check", json={"item": "poulet|masse", "checked": True})
    etat = client.get(f"/api/courses/{pid}/etat").json()
    assert etat == client.get(f"{url}/etat").json()
    assert etat["checked"] == ["poulet|masse"]


def test_etat_page_planning_introuvable(client):
    assert client.get("/api/courses/999/etat").status_code == 404


def test_etat_page_planning_data_json_illisible(client, monkeypatch):
    pid = make_planning(client, monkeypatch, SEL)
    set_data(pid, {})
    assert client.get(f"/api/courses/{pid}/etat").json()["checked"] == []


# ── Gabarits ──────────────────────────────────────────────────────────

def test_partage_possible_avec_seulement_des_articles_manuels(client, monkeypatch):
    pid = make_planning(client, monkeypatch, SEL, generate=False)
    set_data(pid, {**data_of(pid), "liste_courses": []})
    html = client.get(f"/planning/{pid}").text
    assert 'id="shop-share" hidden' in html
    client.post(f"/api/courses/{pid}/extra", json={"texte": "café"})
    html = client.get(f"/planning/{pid}").text
    assert 'id="shop-share" hidden' not in html and 'id="btn-share"' in html
    url = share(client, pid)
    assert 'data-item="extra:cafe|"' in client.get(url).text


def test_bouton_revoquer_seulement_si_un_lien_existe(client, monkeypatch):
    pid = make_planning(client, monkeypatch, SEL)
    assert 'id="btn-unshare" title="Le lien actuel ne fonctionnera plus" hidden' in client.get(f"/planning/{pid}").text
    share(client, pid)
    html = client.get(f"/planning/{pid}").text
    assert 'id="btn-unshare"' in html
    assert 'id="btn-unshare" title="Le lien actuel ne fonctionnera plus" hidden' not in html


def test_modale_du_placard_presente(client, monkeypatch):
    pid = make_planning(client, monkeypatch, SEL)
    html = client.get(f"/planning/{pid}").text
    assert 'id="btn-placard"' in html and 'id="placard-modal"' in html
