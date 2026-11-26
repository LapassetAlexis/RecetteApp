"""L10 — rouvrir un planning (validé ou non) dans le constructeur, historique."""

import asyncio
import json
import re

import pytest
from starlette.testclient import TestClient

from app import main
from tests.test_brouillons_integrite import (
    _catalogue,
    _data,
    _draft_id,
    _initial_builder,
    _planning,
)
from tests.test_routes import _case, _construire, _recipe


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbpath = str(tmp_path / "t.db")
    main.db.path = dbpath
    monkeypatch.setattr(main.settings, "database_path", dbpath)

    async def _noop(*a, **k):
        return True
    monkeypatch.setattr(main.notion, "ensure_ingredients_field", _noop)
    with TestClient(main.app) as c:
        yield c


def _semaine_validee(client, monkeypatch):
    poulet, soupe = _recipe("Poulet", id="p1"), _recipe("Soupe", id="s1")
    _catalogue(monkeypatch, poulet, soupe)
    pid = _construire(client, [_case(1, "midi", poulet, persons=2),
                               _case(1, "soir", soupe, persons=2)])
    client.post(f"/planning/{pid}/valider")
    return pid, poulet, soupe


def test_planning_validé_rouvert_dans_le_constructeur(client, monkeypatch):
    pid, _, _ = _semaine_validee(client, monkeypatch)
    # ?draft= sur une semaine validée : redirigé vers ?planning= (M09)…
    r = client.get(f"/?draft={pid}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/?planning={pid}"
    # … ?planning= l'ouvre.
    html = client.get(f"/?planning={pid}").text
    builder = _initial_builder(html)
    assert _draft_id(html) == str(pid)
    assert next(c for c in builder if c["jour"] == 1 and c["moment"] == "midi")["main"]["nom"] == "Poulet"
    # Planning inexistant / id invalide : constructeur vide.
    assert _initial_builder(client.get("/?planning=99999").text) == []
    assert _initial_builder(client.get("/?planning=abc").text) == []


def test_semaine_validee_modifiee_reste_validee(client, monkeypatch):
    pid, poulet, soupe = _semaine_validee(client, monkeypatch)
    data = _data(pid)
    data["share_token"] = "tok123"
    data["courses_checked"] = ["poulet", "disparu"]
    asyncio.run(main.db.update_planning_data(pid, json.dumps(data)))

    cases = [_case(1, "midi", soupe, persons=3), _case(1, "soir", poulet, persons=3)]
    r = client.post("/construire", data={
        "week_start": "2026-01-05", "meals": json.dumps(cases), "draft_id": str(pid),
    }, follow_redirects=False)
    assert r.headers["location"] == f"/planning/{pid}"
    p = _planning(pid)
    assert p["valide"] == 1
    assert len(asyncio.run(main.db.list_plannings())) == 1
    assert not asyncio.run(main.db.list_drafts())
    d = json.loads(p["data_json"])
    assert d["plats"][0]["nom_recette"] == "Soupe"
    assert d["share_token"] == "tok123"        # lien de partage conservé
    assert "disparu" not in d.get("courses_checked", [])
    # Toujours dans « Semaines validées ».
    hist = client.get("/historique").text
    valid_part = hist.split("Semaines validées", 1)[1]
    assert f"/planning/{pid}" in valid_part


def test_enregistrer_semaine_validee_comme_brouillon_reste_validee(client, monkeypatch):
    pid, poulet, _ = _semaine_validee(client, monkeypatch)
    r = client.post("/construire", data={
        "week_start": "2026-01-05", "action": "brouillon", "draft_id": str(pid),
        "meals": json.dumps([_case(1, "midi", poulet, persons=2)]),
    }, follow_redirects=False)
    assert r.headers["location"] == f"/?planning={pid}&saved=1"
    assert _planning(pid)["valide"] == 1


def test_bouton_modifier_sur_la_page_planning(client, monkeypatch):
    pid, _, _ = _semaine_validee(client, monkeypatch)
    html = client.get(f"/planning/{pid}").text
    assert f'href="/?planning={pid}"' in html and "Modifier dans le constructeur" in html


def test_historique_supprimer_semaine_validee_sans_confirm(client, monkeypatch):
    pid, _, _ = _semaine_validee(client, monkeypatch)
    html = client.get("/historique").text
    valid_part = html.split("Semaines validées", 1)[1]
    assert re.search(rf'class="[^"]*hist-del[^"]*" data-id="{pid}" data-kind="semaine"', valid_part)
    assert f'href="/?planning={pid}"' in valid_part
    assert 'id="del-modal"' in html and 'role="alertdialog"' in html
    assert "confirm(" not in html
    # Aperçu accessible au toucher : bouton 👁 qui déplie un bloc dans la carte.
    assert f'aria-controls="pv-{pid}"' in html and f'id="pv-{pid}"' in html
    assert client.post(f"/planning/{pid}/supprimer").json()["success"]
    assert _planning(pid) is None


def test_historique_pagination_voir_plus(client, monkeypatch):
    _catalogue(monkeypatch)
    for _ in range(23):
        pid = asyncio.run(main.db.save_planning("2026-01-05", "", 2, "", json.dumps({"plats": []}), []))
        asyncio.run(main.db.mark_planning_valid(pid))
    html = client.get("/historique").text
    assert html.count('data-kind="semaine"') == 20
    assert 'href="/historique?n=40"' in html
    html = client.get("/historique?n=40").text
    assert html.count('data-kind="semaine"') == 23
    assert "Voir plus" not in html
    # Valeur aberrante bornée.
    assert client.get("/historique?n=-5").text.count('data-kind="semaine"') == 20
