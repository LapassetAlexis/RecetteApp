"""M11 — socle des tests (fixture unique, horloge figée, taille des fichiers)
et titre du planning aligné sur la période."""

import asyncio
import json
import re
from datetime import date
from pathlib import Path

import app.main as main
from tests.aides import _case, _construire, _recipe

TESTS = Path(__file__).resolve().parent


def test_une_seule_definition_de_la_fixture_client():
    definitions = [p.relative_to(TESTS).as_posix() for p in TESTS.rglob("*.py")
                   if re.search(r"^def (client|make_client)\(", p.read_text(encoding="utf-8"), re.M)]
    assert definitions == ["conftest.py"]


def test_aucun_fichier_de_test_au_dessus_de_500_lignes():
    trop_longs = {p.name: n for p in TESTS.rglob("*.py")
                  if (n := len(p.read_text(encoding="utf-8").splitlines())) > 500}
    assert trop_longs == {}


def test_horloge_figee(aujourdhui):
    from app.routers import pages
    from app.services import recettes
    assert aujourdhui == date(2026, 11, 25)
    assert pages.date.today() == recettes.date.today() == aujourdhui
    # Le reste de `date` est inchangé.
    assert pages.date.fromisoformat("2026-01-05") == date(2026, 1, 5)


def test_titre_du_planning_suit_la_periode(client, monkeypatch):
    """Période de 3 jours dont le 3ᵉ est vide : le titre va jusqu'au 3ᵉ jour,
    comme la grille (et plus seulement jusqu'au dernier jour porteur d'un plat)."""
    poulet = _recipe("Poulet", id="p1")

    async def _all(*a, **k):
        return [poulet]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    cases = [_case(j, m, main=poulet if j <= 2 else None, persons=2 if j <= 3 else 0)
             for j in range(1, 8) for m in ("midi", "soir")]
    pid = _construire(client, cases, week_start="2026-07-13")
    data = json.loads(asyncio.run(main.db.get_planning_with_recipes(pid))["data_json"])
    assert data["nb_jours"] == 3

    html = client.get(f"/planning/{pid}").text
    assert "Lundi 13/07 → mercredi 15/07" in html
    assert "mardi 14/07</" not in html.split("<h1", 1)[-1].split("</h1>", 1)[0]
