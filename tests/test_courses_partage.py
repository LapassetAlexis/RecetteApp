"""L09 — clé d'article, coche depuis le lien public, rafraîchissement."""

import asyncio
import json
import os
import re

import pytest
from starlette.testclient import TestClient

import app.main as main
from app.services.shopping import apply_check, fmt_quantite, item_key, shopping_view
from tests.test_routes import _case, _construire, _recipe


def make_client(tmp_path, monkeypatch) -> TestClient:
    """Client sur une base temporaire, Notion simulé (partagé par les tests L09)."""
    dbpath = str(tmp_path / "t.db")
    main.db.path = dbpath
    monkeypatch.setattr(main.settings, "database_path", dbpath)

    async def _noop(*a, **k):
        return True
    monkeypatch.setattr(main.notion, "ensure_ingredients_field", _noop)
    return TestClient(main.app)


@pytest.fixture
def client(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as c:
        yield c


def make_planning(client, monkeypatch, recettes: dict[str, list[dict]], generate=True) -> int:
    """Planning d'un repas par recette (lundi midi, mardi midi…), ingrédients en
    cache (base 4 personnes, repas pour 4 : quantités inchangées)."""
    recs = [_recipe(nom, id=f"r{i}") for i, nom in enumerate(recettes)]

    async def _all():
        return recs
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    asyncio.run(main.db.init())
    for r in recs:
        asyncio.run(main.db.save_enriched(r["id"], r["nom"],
                                          ingredients=json.dumps(recettes[r["nom"]])))
    pid = _construire(client, [_case(i + 1, "midi", r, persons=4) for i, r in enumerate(recs)])
    if generate:
        assert client.post(f"/generate-shopping/{pid}").json().get("success")
    return pid


def data_of(pid: int) -> dict:
    return json.loads(asyncio.run(main.db.get_planning_with_recipes(pid))["data_json"])


def set_data(pid: int, data: dict) -> None:
    asyncio.run(main.db.update_planning_data(pid, json.dumps(data, ensure_ascii=False)))


def share(client, pid: int) -> str:
    return client.post(f"/planning/{pid}/partager").json()["url"]


def checked_keys(html: str) -> set[str]:
    """Clés des lignes cochées dans le HTML rendu."""
    return set(re.findall(r'data-item="([^"]+)"[^>]*>\s*<input type="checkbox" class="shop-c" checked', html))


LAIT_2_LIGNES = {
    "Gratin": [{"nom": "lait", "quantite": "1", "unite": "l"}],
    "Gâteau": [{"nom": "lait", "quantite": "2", "unite": "pot"}],
}


# ── item_key ──────────────────────────────────────────────────────────

def test_item_key_nom_normalise_et_famille_d_unite():
    assert item_key({"nom": "Lait", "quantite": "1", "unite": "l"}) == "lait|volume"
    assert item_key({"nom": "lait", "quantite": "25", "unite": "cl"}) == "lait|volume"
    assert item_key({"nom": "Œufs", "quantite": "4", "unite": ""}) == "oeuf|"
    assert item_key({"nom": "farine", "quantite": "200", "unite": "g"}) == "farine|masse"
    # quantité non numérique : pas de conversion (comme merge_ingredients)
    assert item_key({"nom": "farine", "quantite": "quelques", "unite": "g"}) == "farine|g"
    assert item_key({"nom": "lait", "quantite": "2", "unite": "pot"}) == "lait|pot"


def test_fmt_quantite_virgule_francaise():
    assert fmt_quantite("1.25") == "1,25"
    assert fmt_quantite("1/2") == "1/2"
    assert fmt_quantite("") == ""
    assert fmt_quantite(None) == ""


def test_liste_generee_porte_la_cle_de_chaque_article(client, monkeypatch):
    pid = make_planning(client, monkeypatch, LAIT_2_LIGNES)
    keys = sorted(it["key"] for it in data_of(pid)["liste_courses"])
    assert keys == ["lait|pot", "lait|volume"]


# ── B7 : deux lignes de même nom se cochent indépendamment ────────────

def test_deux_lignes_meme_nom_cochees_independamment(client, monkeypatch):
    pid = make_planning(client, monkeypatch, LAIT_2_LIGNES)
    r = client.post(f"/api/shopping-check/{pid}", json={"item": "lait|volume", "checked": True})
    assert r.json() == {"success": True, "ignored": False}
    assert checked_keys(client.get(f"/planning/{pid}").text) == {"lait|volume"}
    pub = client.get(share(client, pid)).text
    assert checked_keys(pub) == {"lait|volume"}


def test_ancienne_coche_par_nom_coche_toutes_les_lignes(client, monkeypatch):
    pid = make_planning(client, monkeypatch, LAIT_2_LIGNES)
    data = data_of(pid)
    data["courses_checked"] = ["lait"]          # format d'avant L09
    set_data(pid, data)
    assert checked_keys(client.get(f"/planning/{pid}").text) == {"lait|volume", "lait|pot"}

    # Décocher UNE ligne : l'autre reste cochée, l'ancienne coche est convertie.
    client.post(f"/api/shopping-check/{pid}", json={"item": "lait|pot", "checked": False})
    assert data_of(pid)["courses_checked"] == ["lait|volume"]
    assert checked_keys(client.get(f"/planning/{pid}").text) == {"lait|volume"}


def test_coche_par_nom_seul_toujours_acceptee_cote_planning(client, monkeypatch):
    pid = make_planning(client, monkeypatch, LAIT_2_LIGNES)
    client.post(f"/api/shopping-check/{pid}", json={"item": "lait|pot", "checked": True})
    client.post(f"/api/shopping-check/{pid}", json={"item": "Lait", "checked": True})
    assert "lait" in data_of(pid)["courses_checked"]
    client.post(f"/api/shopping-check/{pid}", json={"item": "lait", "checked": False})
    assert data_of(pid)["courses_checked"] == []
    # clé inconnue (article disparu) : ignorée sans erreur, rien n'est stocké
    r = client.post(f"/api/shopping-check/{pid}", json={"item": "beurre|masse", "checked": True})
    assert r.json() == {"success": True, "ignored": True}
    assert data_of(pid)["courses_checked"] == []


def test_apply_check_strict_refuse_nom_seul_et_cle_inconnue():
    data = {"liste_courses": [{"nom": "riz", "quantite": "100", "unite": "g"}]}
    assert not apply_check(data, "riz", True, strict=True)
    assert not apply_check(data, "pates|masse", True, strict=True)
    assert not apply_check(data, "", True)
    assert apply_check(data, "riz|masse", True, strict=True)
    assert data["courses_checked"] == ["riz|masse"]


# ── Lien public : coche + état ───────────────────────────────────────

def test_coche_publique_visible_sur_le_planning(client, monkeypatch):
    pid = make_planning(client, monkeypatch, LAIT_2_LIGNES)
    url = share(client, pid)
    r = client.post(f"{url}/check", json={"item": "lait|pot", "checked": True})
    assert r.status_code == 200 and r.json() == {"success": True}
    assert checked_keys(client.get(f"/planning/{pid}").text) == {"lait|pot"}
    # page publique : l'article coché est replié sous « Déjà pris »
    pub = client.get(url).text
    restants, pris = pub.split('id="pub-pris"', 1)
    assert 'data-item="lait|pot"' in pris and 'data-item="lait|pot"' not in restants
    assert 'data-item="lait|volume"' in restants
    # décocher depuis le lien public
    client.post(f"{url}/check", json={"item": "lait|pot", "checked": False})
    assert data_of(pid)["courses_checked"] == []


def test_etat_reflete_les_coches_du_planning(client, monkeypatch):
    pid = make_planning(client, monkeypatch, LAIT_2_LIGNES)
    url = share(client, pid)
    e1 = client.get(f"{url}/etat").json()
    assert e1["checked"] == [] and isinstance(e1["version"], int)
    client.post(f"/api/shopping-check/{pid}", json={"item": "lait|volume", "checked": True})
    e2 = client.get(f"{url}/etat").json()
    assert e2["checked"] == ["lait|volume"]
    assert e2["version"] == e1["version"]           # cocher ne change pas la liste
    client.post(f"/api/courses/{pid}/extra", json={"texte": "café"})
    assert client.get(f"{url}/etat").json()["version"] != e1["version"]


def test_jeton_invalide_404_sur_les_trois_routes(client):
    assert client.get("/courses/inconnu").status_code == 404
    assert client.get("/courses/inconnu/etat").status_code == 404
    assert client.post("/courses/inconnu/check", json={"item": "x|", "checked": True}).status_code == 404


def test_lien_public_ne_modifie_que_les_coches(client, monkeypatch):
    pid = make_planning(client, monkeypatch, LAIT_2_LIGNES)
    url = share(client, pid)
    avant = data_of(pid)
    # clé inconnue, nom seul, champs en trop : refusés, rien ne change
    assert client.post(f"{url}/check", json={"item": "inconnu|", "checked": True}).status_code == 400
    assert client.post(f"{url}/check", json={"item": "lait", "checked": True}).status_code == 400
    assert client.post(f"{url}/check", content=b"pas du json").status_code == 400
    assert data_of(pid) == avant
    r = client.post(f"{url}/check", json={"item": "lait|pot", "checked": True,
                                          "liste_courses": [], "share_token": "pirate"})
    assert r.status_code == 200
    apres = data_of(pid)
    assert apres.pop("courses_checked") == ["lait|pot"]
    avant.pop("courses_checked", None)
    assert apres == avant
    # aucune route d'ajout ou de masque sous /courses/<jeton>
    for chemin in ("extra", "masquer", "placard"):
        assert client.post(f"{url}/{chemin}", json={"texte": "x"}).status_code in (404, 405)


def test_quantites_affichees_avec_virgule(client, monkeypatch):
    pid = make_planning(client, monkeypatch, {
        "Purée": [{"nom": "pommes de terre", "quantite": "1.25", "unite": "kg"}],
    })
    assert data_of(pid)["liste_courses"][0]["quantite"] == "1.25"   # stockage inchangé
    assert "<small>1,25 kg</small>" in client.get(f"/planning/{pid}").text
    assert "<small>1,25 kg</small>" in client.get(share(client, pid)).text


def test_changement_de_repas_purge_les_coches_par_cle(client, monkeypatch):
    pid = make_planning(client, monkeypatch, {
        "Poulet": [{"nom": "poulet", "quantite": "200", "unite": "g"},
                   {"nom": "sel", "quantite": "1", "unite": "pincée"}],
        "Curry": [{"nom": "curry", "quantite": "1", "unite": "c. à café"},
                  {"nom": "sel", "quantite": "1", "unite": "pincée"}],
    })
    for key in ("poulet|masse", "sel|pincee"):
        client.post(f"/api/shopping-check/{pid}", json={"item": key, "checked": True})
    client.post(f"/api/planning/{pid}/meals",
                json={"slots": [{"jour": 1, "moment": "midi"}], "action": "set_main", "nom": "Curry"})
    assert data_of(pid)["courses_checked"] == ["sel|pincee"]


def test_shopping_view_tolere_un_planning_sans_cles():
    view = shopping_view({"liste_courses": [{"nom": "Riz", "quantite": "100", "unite": "g"}],
                          "courses_checked": ["riz"]})
    assert view["checked"] == ["riz|masse"] and view["nb_restants"] == 0
    assert view["articles"][0]["rayon"] == "Épicerie salée"
    assert shopping_view({})["nb_total"] == 0


# ── Auth activée : /courses/… public, /api/courses/… protégé ──────────

@pytest.fixture
def auth_env(tmp_path):
    from tests.test_auth import _reload_app
    os.environ.update(DATABASE_PATH=str(tmp_path / "auth.db"), AUTH_USER="chef",
                      AUTH_PASSWORD="secret", NOTION_TOKEN="")
    yield _reload_app()
    for k in ("DATABASE_PATH", "AUTH_USER", "AUTH_PASSWORD", "NOTION_TOKEN"):
        os.environ.pop(k, None)
    _reload_app()


def test_routes_publiques_et_protegees_avec_auth(auth_env):
    m = auth_env
    with TestClient(m.app) as c:
        data = {"plats": [], "share_token": "jeton",
                "liste_courses": [{"nom": "riz", "quantite": "100", "unite": "g"}]}
        pid = asyncio.run(m.db.save_planning(
            week_start="2026-01-05", saison="", nb_personnes=2, ingredients_force="",
            data_json=json.dumps(data), recipes=[]))
        assert c.get("/courses/jeton").status_code == 200
        assert c.get("/courses/jeton/etat").status_code == 200
        assert c.post("/courses/jeton/check",
                      json={"item": "riz|masse", "checked": True}).status_code == 200
        for method, path in (("post", f"/api/courses/{pid}/extra"),
                             ("delete", f"/api/courses/{pid}/extra/x"),
                             ("post", f"/api/courses/{pid}/masquer"),
                             ("post", f"/api/courses/{pid}/placard")):
            r = getattr(c, method)(path, follow_redirects=False)
            assert r.status_code == 401, path
