"""M02 — écritures concurrentes d'un planning (N2).

Toutes les écritures du data_json passent par `store.update_planning`, sous le
verrou du planning. Les requêtes simultanées sont jouées dans une même boucle
asyncio (client ASGI), comme deux téléphones sur le même serveur."""

import asyncio
import json

import httpx
import pytest

from app import main
from app.services import shopping_list, store
from tests.aides import _case, _construire, _recipe
from tests.test_brouillons_integrite import _catalogue, _data, _planning

# Sept ingrédients du poulet (jour 1 midi) : les articles cochés pendant que le
# soir du jour 4 change de plat.
POULET_INGS = [
    {"nom": n, "quantite": "100", "unite": "g"}
    for n in ("poulet", "boeuf", "lardon", "tomate", "carotte", "oignon", "courgette")
]
POULET, CURRY = _recipe("Poulet", id="p1"), _recipe("Curry", id="c1")
RIZ, HARICOTS = _recipe("Riz", id="r1"), _recipe("Haricots verts sautés", id="h1")


@pytest.fixture(autouse=True)
def _sans_limite_de_coches(monkeypatch):
    # M06 : les coches publiques sont limitées à 120 par minute et par jeton ;
    # le scénario de charge en envoie 280 d'affilée.
    monkeypatch.setattr("app.routers.courses.CHECK_LIMIT", 10**6)


def _setup(client, monkeypatch) -> int:
    _catalogue(monkeypatch, POULET, CURRY, RIZ, HARICOTS)
    ings = {"p1": POULET_INGS, "c1": [{"nom": "curry", "quantite": "1", "unite": "pot"}],
            "r1": [{"nom": "riz", "quantite": "100", "unite": "g"}],
            "h1": [{"nom": "haricots verts", "quantite": "200", "unite": "g"}]}
    for nid, lst in ings.items():
        asyncio.run(main.db.save_enriched(nid, nid, ingredients=json.dumps(lst)))
    return _construire(client, [_case(1, "midi", POULET, persons=4),
                                _case(4, "soir", CURRY, persons=4)])


def _asgi() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://t")


def _keys(pid: int) -> list[str]:
    keys = {i["key"] for i in _data(pid)["liste_courses"]}
    return sorted(k for k in keys if k.split("|")[0] in {i["nom"] for i in POULET_INGS})


# ── Le scénario de l'audit (race.py) : 40 tours ─────────────────────────


def test_changement_de_repas_et_coches_simultanes_rien_n_est_perdu(client, monkeypatch):
    pid = _setup(client, monkeypatch)
    token = client.post(f"/planning/{pid}/partager").json()["url"]
    keys = _keys(pid)
    assert len(keys) == 7

    perdus = {"repas": 0, "incoherent": 0, "coche": 0}

    async def _tours():
        async with _asgi() as ac:
            for n in range(40):
                nom = "Riz" if n % 2 == 0 else "Haricots verts sautés"
                checks = [ac.post(f"{token}/check", json={"item": k, "checked": True}) for k in keys]
                meal = ac.post(f"/api/planning/{pid}/meals", json={
                    "slots": [{"jour": 4, "moment": "soir"}], "action": "set_main", "nom": nom})
                reps = await asyncio.gather(*checks[:3], meal, *checks[3:])
                assert all(r.status_code == 200 for r in reps)

                p = await main.db.get_planning_with_recipes(pid)
                data = json.loads(p["data_json"])
                plat = next(x for x in data["plats"] if x["jour"] == 4 and x["moment"] == "soir")
                rec = next(r for r in p["recipes"] if r["jour"] == 4 and r["moment"] == "soir")
                perdus["repas"] += plat["nom_recette"] != nom
                perdus["incoherent"] += plat["nom_recette"] != rec["recipe_name"]
                perdus["coche"] += bool(set(keys) - set(data.get("courses_checked", [])))
                await asyncio.gather(*[
                    ac.post(f"/api/shopping-check/{pid}", json={"item": k, "checked": False})
                    for k in keys])

    asyncio.run(_tours())
    assert perdus == {"repas": 0, "incoherent": 0, "coche": 0}


# ── Autres écritures : génération, partage, création, placard ───────────


def test_generation_de_la_liste_et_ajout_manuel_simultanes(client, monkeypatch):
    pid = _setup(client, monkeypatch)

    async def _go():
        async with _asgi() as ac:
            await asyncio.gather(
                ac.post(f"/generate-shopping/{pid}"),
                *[ac.post(f"/api/courses/{pid}/extra", json={"texte": f"article {i}"})
                  for i in range(5)],
            )

    asyncio.run(_go())
    data = _data(pid)
    assert len(data["courses_extra"]) == 5
    assert {i["nom"] for i in data["liste_courses"]} >= {"poulet", "curry"}


def test_partage_simultane_un_seul_jeton(client, monkeypatch):
    pid = _setup(client, monkeypatch)

    async def _go():
        async with _asgi() as ac:
            return await asyncio.gather(*[ac.post(f"/planning/{pid}/partager") for _ in range(6)])

    urls = {r.json()["url"] for r in asyncio.run(_go())}
    assert len(urls) == 1
    assert urls == {f"/courses/{_data(pid)['share_token']}"}


def test_repas_libre_cree_hors_verrou_et_coches_conservees(client, monkeypatch):
    """La création Notion (lente) ne bloque pas les coches, et le placement
    relit le planning : les coches faites pendant la création restent."""
    pid = _setup(client, monkeypatch)
    keys = _keys(pid)

    async def _go():
        started = asyncio.Event()

        async def _create(**kw):
            started.set()
            await asyncio.sleep(0.05)
            return {"id": "libre1", "url": "https://notion/libre1"}

        async def _noop(*a, **k):
            return True
        monkeypatch.setattr(main.notion, "create_recipe", _create)
        monkeypatch.setattr(main.notion, "update_ingredients", _noop)

        async with _asgi() as ac:
            async def _cocher():
                await started.wait()
                return await asyncio.gather(*[
                    ac.post(f"/api/shopping-check/{pid}", json={"item": k, "checked": True})
                    for k in keys])
            res, _ = await asyncio.gather(
                ac.post(f"/api/free-meal/{pid}", json={
                    "jour": 4, "moment": "soir", "nom": "Pizzas", "ingredients": "1 pâte"}),
                _cocher(),
            )
            return res.json()

    res = asyncio.run(_go())
    assert res.get("success")
    data = _data(pid)
    assert set(keys) <= set(data["courses_checked"])
    soir = next(p for p in data["plats"] if p["jour"] == 4 and p["moment"] == "soir")
    assert soir["nom_recette"] == "Pizzas"
    assert {r["recipe_name"] for r in _planning(pid)["recipes"]} == {"Poulet", "Pizzas"}


def test_repas_libre_creneau_retire_pendant_la_creation(client, monkeypatch):
    """Le créneau disparaît entre la vérification et l'écriture : rien n'est
    écrit et la recette Notion créée est archivée."""
    pid = _setup(client, monkeypatch)
    archives = []
    data = _data(pid)

    async def _create(**kw):
        # Pendant la création, le soir du jour 4 est retiré du planning.
        data["plats"] = [p for p in data["plats"] if p["jour"] != 4]
        await main.db.update_planning_data(pid, json.dumps(data))
        return {"id": "libre2", "url": ""}

    async def _archive(page_id):
        archives.append(page_id)

    async def _noop(*a, **k):
        return True
    monkeypatch.setattr(main.notion, "create_recipe", _create)
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)
    monkeypatch.setattr(main.notion, "archive_recipe", _archive)

    res = client.post(f"/api/free-meal/{pid}", json={"jour": 4, "moment": "soir", "nom": "Pizzas"}).json()
    assert res == {"error": "Repas non trouvé dans le planning"}
    assert archives == ["libre2"]
    assert all(p["nom_recette"] != "Pizzas" for p in _data(pid)["plats"])


def test_placard_ajouts_simultanes_tous_gardes(client):
    async def _go():
        await asyncio.gather(*[shopping_list.set_placard(f"article {i}", True) for i in range(8)])
        return await shopping_list.get_placard()

    assert len(asyncio.run(_go())) == 8


# ── store.update_planning ──────────────────────────────────────────────


def test_update_planning_sans_ecriture_et_planning_absent(client, monkeypatch):
    pid = _setup(client, monkeypatch)
    avant = _planning(pid)["data_json"]

    def _rien(planning, data):
        data["plats"] = []
        return store.NoWrite("motif")

    assert asyncio.run(store.update_planning(pid, _rien))[1] == "motif"
    assert _planning(pid)["data_json"] == avant
    assert asyncio.run(store.update_planning(99999, _rien)) == (None, None)


def test_update_planning_fonction_asynchrone_et_recettes(client, monkeypatch):
    pid = _setup(client, monkeypatch)

    async def _vider(planning, data):
        await asyncio.sleep(0)
        data["plats"] = [p for p in data["plats"] if p["jour"] == 1]
        return "ok"

    data, res = asyncio.run(store.update_planning(pid, _vider, recipes=True))
    assert res == "ok" and len(data["plats"]) == 1
    assert [r["recipe_name"] for r in _planning(pid)["recipes"]] == ["Poulet"]


def test_mutate_planning_data_passe_par_le_verrou(client, monkeypatch):
    pid = _setup(client, monkeypatch)
    pris = []
    real = store.planning_lock

    def _spy(planning_id):
        pris.append(planning_id)
        return real(planning_id)
    monkeypatch.setattr(store, "planning_lock", _spy)
    client.post(f"/api/shopping-check/{pid}", json={"item": _keys(pid)[0], "checked": True})
    client.post(f"/api/planning/{pid}/meals", json={
        "slots": [{"jour": 4, "moment": "soir"}], "action": "set_main", "nom": "Riz"})
    client.post(f"/generate-shopping/{pid}")
    client.post(f"/planning/{pid}/partager")
    assert pris == [pid] * 4
