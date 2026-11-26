"""Routes des pages générales (accueil, historique, catalogue), Notion simulé."""



import app.main as main
from tests.aides import _recipe


def test_index_and_historique(client):
    assert client.get("/").status_code == 200
    assert client.get("/historique").status_code == 200
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/sw.js").status_code == 200
    assert client.get("/static/manifest.json").status_code == 200


def test_api_catalogue(client, monkeypatch):
    async def _all():
        return [_recipe("Poulet", id="p1", base=["Viande"]),
                _recipe("Riz", id="r1", repas="", base=["Féculent"], nature="Ingrédient")]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    data = client.get("/api/catalogue").json()
    assert isinstance(data, list) and len(data) == 2
    poulet = next(x for x in data if x["nom"] == "Poulet")
    assert poulet["id"] == "p1" and poulet["nature"] == "Recette"
    assert poulet["base"] == ["Viande"]
    riz = next(x for x in data if x["nom"] == "Riz")
    assert riz["nature"] == "Ingrédient" and riz["base"] == ["Féculent"]
