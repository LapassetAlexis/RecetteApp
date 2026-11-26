"""Sauvegarde / restauration : export JSON, import (fusion, remplacement),
fichiers invalides, page « Données » et script de cron."""

import asyncio
import json
import re
from datetime import datetime

import pytest

import app.main as main
from app.database import Database
from app.services import backup
from scripts import backup as backup_script
from tests import test_routes
from tests.test_routes import _case, _construire, _recipe

client = test_routes.client  # fixture réutilisée


def _db(path) -> Database:
    db = Database()
    db.path = str(path)
    asyncio.run(db.init())
    return db


def _mock_notion(monkeypatch):
    poulet, soupe = _recipe("Poulet", id="p1"), _recipe("Soupe", id="s1")

    async def _all(*a, **k):
        return [poulet, soupe]

    async def _noop(*a, **k):
        return {}
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.notion, "update_ingredients", _noop)
    return poulet, soupe


def _peupler(client):
    """Une semaine validée avec liste de courses cochée et partagée, un
    brouillon, deux recettes en cache et des réglages. Renvoie (pid, token)."""
    poulet, soupe = _recipe("Poulet", id="p1"), _recipe("Soupe", id="s1")
    asyncio.run(main.db.save_enriched(
        "p1", "Poulet", ingredients=json.dumps([{"nom": "riz", "quantite": "100", "unite": "g"}]),
        cuisson_minutes=30, base_servings=4))
    asyncio.run(main.db.save_enriched(
        "s1", "Soupe", ingredients=json.dumps([{"nom": "carottes", "quantite": "3", "unite": ""},
                                               {"nom": "riz", "quantite": "50", "unite": "g"}]),
        base_servings=2))
    pid = _construire(client, [_case(1, "midi", poulet, persons=2),
                               _case(2, "soir", soupe, persons=3)])
    assert client.post(f"/generate-shopping/{pid}").json().get("success")
    assert client.post(f"/api/shopping-check/{pid}", json={"item": "carottes", "checked": True}).json()["success"]
    token = client.post(f"/planning/{pid}/partager").json()["url"].rsplit("/", 1)[1]
    client.post(f"/planning/{pid}/valider")
    _construire(client, [_case(3, "soir", poulet)], week_start="2026-02-02")  # brouillon
    asyncio.run(main.db.save_app_prefs({"placard": ["sel", "poivre"]}))
    return pid, token


def _pages(client, pid, token):
    return client.get(f"/planning/{pid}").text, client.get(f"/courses/{token}").text


def _importer(client, contenu: bytes, mode: str):
    """Les deux temps de l'import : envoi du fichier, puis confirmation."""
    r = client.post("/admin/import", data={"mode": mode},
                    files={"fichier": ("sauvegarde.json", contenu, "application/json")})
    assert r.status_code == 200, r.text
    jeton = re.search(r'name="jeton" value="([^"]+)"', r.text).group(1)
    return r, client.post("/admin/import", data={"jeton": jeton, "mode": mode})


def test_export_replace_sur_base_vide_restitue_les_pages(client, monkeypatch, tmp_path):
    _mock_notion(monkeypatch)
    pid, token = _peupler(client)
    avant = _pages(client, pid, token)
    data_avant = asyncio.run(main.db.dump_all())

    r = client.get("/admin/export")
    assert r.status_code == 200
    assert re.search(r'filename="menu-planner-\d{4}-\d{2}-\d{2}\.json"', r.headers["content-disposition"])
    export = r.json()
    assert export["version"] == 1 and export["exported_at"]
    assert len(export["plannings"]) == 2 and len(export["enriched"]) == 2
    assert export["prefs"] == {"placard": ["sel", "poivre"]}

    # Nouvelle base vide.
    vide = str(tmp_path / "vide.db")
    main.db.path = vide
    monkeypatch.setattr(main.settings, "database_path", vide)
    asyncio.run(main.db.init())
    assert client.get(f"/courses/{token}").status_code == 404

    recap = client.post("/admin/import", data={"mode": "replace"},
                        files={"fichier": ("sauvegarde.json", r.content, "application/json")})
    assert "Vérifier avant d'importer" in recap.text and "Mode remplacement" in recap.text
    assert asyncio.run(main.db.counts())["plannings"] == 0  # rien d'écrit avant confirmation
    jeton = re.search(r'name="jeton" value="([^"]+)"', recap.text).group(1)
    fin = client.post("/admin/import", data={"jeton": jeton, "mode": "replace"})
    assert fin.status_code == 200 and "Import en remplacement appliqué" in fin.text

    assert asyncio.run(main.db.dump_all()) == data_avant
    assert _pages(client, pid, token) == avant
    assert "carottes" not in avant[1] and "riz" in avant[1]  # coche conservée


def test_import_fichier_invalide_ne_modifie_rien(client, monkeypatch):
    _mock_notion(monkeypatch)
    _peupler(client)
    avant = asyncio.run(main.db.dump_all())
    mauvais = [
        b"pas du json",
        json.dumps({"version": 99, "plannings": [], "enriched": []}).encode(),
        json.dumps({"version": 1, "plannings": [{"id": 1, "week_start": "2026-01-05",
                                                 "data_json": "{cassé"}], "enriched": []}).encode(),
        json.dumps({"version": 1, "plannings": [], "enriched": [{"recipe_name": "x"}]}).encode(),
        json.dumps([1, 2]).encode(),
    ]
    for contenu in mauvais:
        for mode in ("replace", "merge"):
            r = client.post("/admin/import", data={"mode": mode},
                            files={"fichier": ("x.json", contenu, "application/json")})
            assert r.status_code == 400 and "Fichier refusé" in r.text
    assert asyncio.run(main.db.dump_all()) == avant


def test_import_all_invalide_leve_sans_ecrire(tmp_path):
    db = _db(tmp_path / "a.db")
    asyncio.run(db.save_app_prefs({"placard": ["sel"]}))
    with pytest.raises(backup.BackupError):
        asyncio.run(backup.import_all(db, {"version": 1, "plannings": "x", "enriched": []}, "replace"))
    with pytest.raises(backup.BackupError):
        asyncio.run(backup.import_all(db, {"version": 1, "plannings": [], "enriched": []}, "tout"))
    assert asyncio.run(db.get_app_prefs()) == {"placard": ["sel"]}


def test_import_echoue_en_cours_annule_tout(tmp_path):
    """Erreur SQL au milieu de l'import (contrainte NOT NULL) : rollback complet."""
    db = _db(tmp_path / "a.db")
    asyncio.run(db.save_planning("2026-01-05", "", 4, "", "{}", []))
    avant = asyncio.run(db.dump_all())
    with pytest.raises(Exception):  # noqa: B017
        asyncio.run(db.restore_all(
            [{"id": 1, "week_start": "2026-03-02", "created_at": "x", "data_json": "{}", "valide": 1,
              "nb_personnes": 4, "recipes": [{"recipe_name": None, "jour": 1, "moment": "soir"}]}],
            [], {}, replace=True))
    assert asyncio.run(db.dump_all()) == avant


def test_import_fusion_ajoute_sans_ecraser(client, monkeypatch, tmp_path):
    _mock_notion(monkeypatch)
    _peupler(client)
    export = client.get("/admin/export").content

    # Base cible avec ses propres données.
    autre = str(tmp_path / "autre.db")
    main.db.path = autre
    monkeypatch.setattr(main.settings, "database_path", autre)
    asyncio.run(main.db.init())
    asyncio.run(main.db.save_enriched("p1", "Poulet local", ingredients=json.dumps([{"nom": "pâtes"}])))
    asyncio.run(main.db.save_app_prefs({"placard": ["huile"]}))
    local_id = asyncio.run(main.db.save_planning("2025-12-01", "", 4, "", "{}", []))

    _, fin = _importer(client, export, "merge")
    assert "Import en fusion appliqué" in fin.text
    assert "2 planning(s) et 1 recette(s)" in fin.text
    assert json.loads(asyncio.run(main.db.get_enriched("p1"))["ingredients"]) == [{"nom": "pâtes"}]
    assert asyncio.run(main.db.get_enriched("s1"))["recipe_name"] == "Soupe"
    assert asyncio.run(main.db.get_app_prefs()) == {"placard": ["huile"]}
    assert asyncio.run(main.db.get_planning_with_recipes(local_id))
    counts = asyncio.run(main.db.counts())
    assert counts == {"plannings": 1, "brouillons": 2, "enriched": 2}

    # Ré-import identique : rien de neuf.
    _, fin = _importer(client, export, "merge")
    assert "0 planning(s) et 0 recette(s)" in fin.text


def test_import_fusion_ajoute_les_reglages_absents(tmp_path):
    db = _db(tmp_path / "a.db")
    asyncio.run(db.save_app_prefs({"placard": ["huile"]}))
    data = {"version": 1, "plannings": [], "enriched": [], "prefs": {"placard": ["sel"], "ordre": ["Frais"]}}
    asyncio.run(backup.import_all(db, data, "merge"))
    assert asyncio.run(db.get_app_prefs()) == {"placard": ["huile"], "ordre": ["Frais"]}


def test_validate_accepte_data_json_objet_et_resume():
    clean = backup.validate_backup({
        "version": 1, "exported_at": "2026-11-26T10:00:00",
        "plannings": [{"id": 3, "week_start": "2026-01-05", "data_json": {"liste_courses": [{"nom": "riz"}]},
                       "valide": 0}],
        "enriched": [{"notion_id": "a", "recipe_name": "A"}],
    })
    p = clean["plannings"][0]
    assert isinstance(p["data_json"], str) and p["nb_personnes"] == 4 and p["valide"] == 0
    assert backup.summarize(clean) == {
        "exported_at": "2026-11-26T10:00:00", "plannings": 0, "brouillons": 1, "listes": 1,
        "enriched": 1, "prefs": [],
    }


@pytest.mark.parametrize("data, message", [
    ({"version": 1, "plannings": [], "enriched": [], "prefs": []}, "prefs"),
    ({"version": 1, "plannings": [1], "enriched": []}, "objet attendu"),
    ({"version": 1, "plannings": [{"week_start": "x", "data_json": "{}"}], "enriched": []}, "id"),
    ({"version": 1, "plannings": [{"id": 1, "data_json": "{}"}], "enriched": []}, "week_start"),
    ({"version": 1, "plannings": [{"id": 1, "week_start": "x", "data_json": "{}", "recipes": {}}],
      "enriched": []}, "liste"),
    ({"version": 1, "plannings": [{"id": 1, "week_start": "x", "data_json": "{}",
                                   "recipes": [{"recipe_name": "a", "jour": "1", "moment": "soir"}]}],
      "enriched": []}, "repas"),
    ({"version": 1, "plannings": [{"id": 1, "week_start": "x", "data_json": "{}", "nb_personnes": "4"}],
      "enriched": []}, "entiers"),
    ({"version": 1, "plannings": [{"id": 1, "week_start": "x", "data_json": "{}"},
                                  {"id": 1, "week_start": "y", "data_json": "{}"}], "enriched": []}, "double"),
    ({"version": 1, "plannings": [], "enriched": [1]}, "objet attendu"),
    ({"version": 1, "plannings": [], "enriched": [{"notion_id": "a"}]}, "recipe_name"),
    ({"version": 1, "plannings": [], "enriched": [{"notion_id": "a", "recipe_name": "A",
                                                    "base_servings": "4"}]}, "base_servings"),
    ({"version": 1, "plannings": [], "enriched": [{"notion_id": "a", "recipe_name": "A"},
                                                   {"notion_id": "a", "recipe_name": "B"}]}, "double"),
])
def test_validate_refuse(data, message):
    with pytest.raises(backup.BackupError, match=message):
        backup.validate_backup(data)


def test_admin_page_et_erreurs(client, monkeypatch):
    r = client.get("/admin")
    assert r.status_code == 200 and "Exporter" in r.text and "/static/css/admin.css" in r.text
    assert 'href="/admin"' in client.get("/").text  # lien « Données » du pied de page
    # Ni fichier ni jeton.
    assert client.post("/admin/import", data={"mode": "merge"}).status_code == 400
    # Mode inconnu.
    assert client.post("/admin/import", data={"mode": "tout"}).status_code == 400
    # Jeton inconnu / déjà utilisé.
    r = client.post("/admin/import", data={"jeton": "inconnu", "mode": "merge"})
    assert r.status_code == 400 and "expiré" in r.text
    # Fichier trop gros.
    monkeypatch.setattr("app.routers.admin.MAX_UPLOAD", 10)
    r = client.post("/admin/import", data={"mode": "merge"},
                    files={"fichier": ("x.json", b"{" * 20, "application/json")})
    assert r.status_code == 400 and "volumineux" in r.text


def test_jeton_import_a_usage_unique(client):
    contenu = json.dumps({"version": 1, "plannings": [], "enriched": [], "prefs": {}}).encode()
    _, fin = _importer(client, contenu, "merge")
    assert fin.status_code == 200
    jeton_rejoue = client.post("/admin/import", data={"jeton": "x", "mode": "merge"})
    assert jeton_rejoue.status_code == 400


def test_pending_purge(monkeypatch):
    from app.routers import admin
    monkeypatch.setattr(admin, "_PENDING", {})
    for i in range(admin._PENDING_MAX + 3):
        admin._PENDING[str(i)] = (0.0 if i == 0 else 1e18, {}, "merge")
    admin._purge_pending()
    assert "0" not in admin._PENDING and len(admin._PENDING) == admin._PENDING_MAX


# ── Script de cron ─────────────────────────────────────────────────

def test_script_backup_ecrit_et_tourne(tmp_path, monkeypatch):
    dbpath = tmp_path / "s.db"
    monkeypatch.setattr("app.database.settings.database_path", str(dbpath))
    db = _db(dbpath)
    asyncio.run(db.save_app_prefs({"placard": ["sel"]}))
    dossier = tmp_path / "backups"
    for jour in range(1, 11):
        asyncio.run(backup_script.run(dossier, keep=8, now=datetime(2026, 11, jour, 3, 0, 0)))
    fichiers = sorted(p.name for p in dossier.iterdir())
    assert len(fichiers) == 8
    assert fichiers[0] == "menu-planner-2026-11-03-030000.json"
    assert fichiers[-1] == "menu-planner-2026-11-10-030000.json"
    contenu = json.loads((dossier / fichiers[-1]).read_text(encoding="utf-8"))
    assert backup.validate_backup(contenu)["prefs"] == {"placard": ["sel"]}


def test_script_backup_main(tmp_path, monkeypatch):
    monkeypatch.setattr("app.database.settings.database_path", str(tmp_path / "m.db"))
    backup_script.main([str(tmp_path / "b"), "--keep", "0"])
    assert len(list((tmp_path / "b").glob("menu-planner-*.json"))) == 1


def test_export_filename():
    assert backup.export_filename(datetime(2026, 1, 5)) == "menu-planner-2026-01-05.json"
