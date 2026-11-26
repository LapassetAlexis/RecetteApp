"""Routes du planning (constructeur, brouillons, affichage, libellés, nutrition), Notion simulé."""

import asyncio
import json

import app.main as main
from app.services import nutrition_week
from app.services import planning as planning_svc
from tests.aides import _case, _construire, _make_shopping_planning, _recipe


def test_construire_success_then_view(client, monkeypatch):
    poulet, soupe = _recipe("Poulet", id="p1"), _recipe("Soupe", id="s1")
    async def _all():
        return [poulet, soupe]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    cases = [_case(1, "midi", poulet, persons=2), _case(1, "soir", soupe, persons=2)]
    r = client.post("/construire", data={"week_start": "2026-01-05",
                                         "meals": json.dumps(cases)},
                    follow_redirects=False)
    assert r.status_code == 303
    assert client.get(r.headers["location"]).status_code == 200


def test_construire_repas_libre(client, monkeypatch):
    """Repas libre : titre saisi à la main (notion_id vide) → plat marqué `libre`,
    aucun ingrédient dans les courses, rappel affiché sur la page planning."""
    async def _all():
        return [_recipe("Poulet", id="p1")]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    # Case avec un main libre (pas de notion_id) : « Pizzas » le samedi soir.
    case = {"jour": 6, "moment": "soir", "persons": 3,
            "main": {"notion_id": "", "nom": "Pizzas", "nature": "Libre", "libre": True},
            "accompagnements": []}
    pid = _construire(client, [case])

    # Le plat est bien marqué libre en base.
    data = json.loads(asyncio.run(main.db.get_planning_with_recipes(pid))["data_json"])
    plat = next(p for p in data["plats"] if p["nom_recette"] == "Pizzas")
    assert plat["libre"] is True and plat["notion_id"] == ""
    # Un repas libre ne produit aucun ingrédient de courses.
    assert data["liste_courses"] == []

    # La page planning affiche le rappel « Repas libres ».
    html = client.get(f"/planning/{pid}").text
    assert "Repas libre" in html and "Pizzas" in html


def test_construire_accompagnement_libre(client, monkeypatch):
    """Accompagnement libre : titre saisi à la main (sans notion_id) → marqué
    `libre`, aucun ingrédient dans les courses, rappelé dans « Repas libres »."""
    poulet = _recipe("Poulet", id="p1")

    async def _all():
        return [poulet]

    async def _enriched(nid):
        return {"ingredients": json.dumps([{"nom": "riz", "quantite": "100", "unite": "g"}])}

    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)

    case = _case(1, "midi", poulet, persons=4)
    case["accompagnements"] = [{"notion_id": "", "nom": "Salade du jardin",
                                "nature": "Libre", "libre": True}]
    pid = _construire(client, [case])

    data = json.loads(asyncio.run(main.db.get_planning_with_recipes(pid))["data_json"])
    acc = data["plats"][0]["accompagnements"][0]
    assert acc["libre"] is True and acc["notion_id"] == ""
    # Seul le plat (enrichi) alimente les courses : pas d'ingrédient inventé.
    assert [i["nom"] for i in data["liste_courses"]] == ["riz"]

    html = client.get(f"/planning/{pid}").text
    assert "Repas libres" in html and "Salade du jardin" in html


def test_planning_case_fusionnee_affiche_les_parts(client, monkeypatch):
    """Midis groupés : la case fusionnée annonce le total de parts comptées
    dans la liste de courses (3 jours × 2 convives = 6 parts)."""
    poulet = _recipe("Poulet", id="p1")

    async def _all():
        return [poulet]

    async def _enriched(nid):
        return {"ingredients": json.dumps([{"nom": "riz", "quantite": "100", "unite": "g"}])}

    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)

    # Le front n'envoie le repas que sur le jour éditeur du groupe (j1).
    pid = _construire(client, [
        _case(1, "midi", poulet, persons=2, group=1),
        _case(2, "midi", None, persons=2, group=1),
        _case(3, "midi", None, persons=2, group=1),
    ])

    data = json.loads(asyncio.run(main.db.get_planning_with_recipes(pid))["data_json"])
    riz = next(i for i in data["liste_courses"] if i["nom"] == "riz")
    # 3 repas × 2 pers × 100 g / base 4 = 150 g
    assert riz["quantite"] == "150"
    assert "6 parts · 3 j" in client.get(f"/planning/{pid}").text


def test_construire_sans_repas(client, monkeypatch):
    """Aucun repas choisi → on re-affiche le formulaire avec un message."""
    async def _all():
        return [_recipe("Poulet", id="p1")]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    # une case avec convives mais sans plat principal → ignorée → aucun plat
    cases = [_case(1, "midi", None, persons=2)]
    r = client.post("/construire", data={"week_start": "2026-01-05",
                                         "meals": json.dumps(cases)})
    assert r.status_code == 400
    assert "Aucun repas" in r.text


def test_construire_error_preserves_week(client, monkeypatch):
    async def _boom():
        raise RuntimeError("Notion down")
    monkeypatch.setattr(main.notion, "get_all_recipes", _boom)
    cases = [_case(1, "midi", _recipe("X", id="x"), persons=2)]
    r = client.post("/construire", data={"week_start": "2026-02-09",
                                         "meals": json.dumps(cases)})
    assert r.status_code == 500
    assert "2026-02-09" in r.text        # la semaine saisie est repassée au formulaire


def test_construire_complet(client, monkeypatch):
    """Constructeur de bout en bout : une recette, une brique + accompagnements,
    une case absente et deux midis groupés partageant le repas. Vérifie les
    plats créés, l'application des groupes, les personnes par repas, la liste de
    courses (ingrédients ancrés au cache) et la redirection /planning."""

    poulet = _recipe("Poulet", id="p1", base=["Viande"])
    steak = _recipe("Steak", id="st", repas="", base=["Viande"], nature="Ingrédient")
    riz = _recipe("Riz", id="rz", repas="", base=["Féculent"], nature="Ingrédient")
    brocoli = _recipe("Brocoli", id="br", repas="", base=["Légume"], nature="Ingrédient")

    async def _all():
        return [poulet, steak, riz, brocoli]
    async def _enriched(nid):
        return {"ingredients": json.dumps([{"nom": nid, "quantite": "100", "unite": "g"}])}
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    monkeypatch.setattr(main.db, "get_enriched", _enriched)

    cases = [
        # lun + mar midi : même groupe 1, seul lun porte le repas (Poulet, 2 pers)
        _case(1, "midi", poulet, persons=2, group=1),
        _case(2, "midi", None, persons=2, group=1),
        # lun soir : brique Steak + accompagnements Riz & Brocoli, 4 pers
        _case(1, "soir", steak, persons=4, accs=[riz, brocoli]),
        # mar soir : absent (0 convive) → ignoré
        _case(2, "soir", poulet, persons=0),
    ]
    pid = _construire(client, cases)

    plats = json.loads(asyncio.run(main.db.get_planning_with_recipes(pid))["data_json"])["plats"]
    by_key = {(p["jour"], p["moment"]): p for p in plats}

    # Case absente non générée ; 3 repas produits (2 midis groupés + 1 soir).
    assert (2, "soir") not in by_key
    assert set(by_key) == {(1, "midi"), (2, "midi"), (1, "soir")}

    # Groupe appliqué : les 2 midis partagent le repas du 1er rempli (Poulet).
    assert by_key[(1, "midi")]["nom_recette"] == "Poulet"
    assert by_key[(2, "midi")]["nom_recette"] == "Poulet"

    # Personnes PAR REPAS respectées.
    assert by_key[(1, "midi")]["persons"] == 2
    assert by_key[(2, "midi")]["persons"] == 2
    assert by_key[(1, "soir")]["persons"] == 4

    # La brique + ses accompagnements sont posés au soir.
    soir = by_key[(1, "soir")]
    assert soir["nature"] == "Ingrédient" and soir["nom_recette"] == "Steak"
    assert {a["nom_recette"] for a in soir["accompagnements"]} == {"Riz", "Brocoli"}

    # Liste de courses générée, mise à l'échelle PAR REPAS.
    shop = {i["nom"]: i for i in json.loads(
        asyncio.run(main.db.get_planning_with_recipes(pid))["data_json"])["liste_courses"]}
    # p1 : 2 midis (2 pers) × 100 g / 4 = 50 g chacun → fusionnés = 100 g.
    assert shop["p1"]["quantite"] == "100"
    # brique + accompagnements soir (4 pers) × 100 g / 4 = 100 g.
    assert shop["st"]["quantite"] == "100"
    assert shop["rz"]["quantite"] == "100"
    assert shop["br"]["quantite"] == "100"


def test_planning_draft_then_validate(client, monkeypatch):
    poulet, soupe = _recipe("Poulet", id="p1"), _recipe("Soupe", id="s1")
    async def _all():
        return [poulet, soupe]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    pid = _construire(client, [_case(1, "midi", poulet, persons=2),
                               _case(1, "soir", soupe, persons=2)])
    # brouillon : listé dans la section Brouillons, PAS dans les validées ;
    # bandeau de validation présent sur la page planning.
    hist = client.get("/historique").text
    assert "📝 Brouillons" in hist
    assert "Semaines validées" not in hist
    assert "Valider la semaine" in client.get(f"/planning/{pid}").text
    # validation → rejoint les semaines validées
    assert client.post(f"/planning/{pid}/valider").json().get("success")
    assert "Semaines validées" in client.get("/historique").text
    assert "Semaine validée" in client.get(f"/planning/{pid}").text


def _enregistrer_brouillon(client, cases, week_start="2026-01-05", draft_id=""):
    """POST /construire avec action=brouillon ; renvoie la réponse (sans suivre
    la redirection) pour inspecter le Location."""
    return client.post("/construire", data={
        "week_start": week_start, "meals": json.dumps(cases),
        "action": "brouillon", "draft_id": draft_id,
    }, follow_redirects=False)


def test_enregistrer_brouillon_reste_en_brouillon(client, monkeypatch):
    """action=brouillon → planning valide=0 avec data["builder"], redirection
    vers /?draft=<id>&saved=1, et PAS de validation."""
    poulet = _recipe("Poulet", id="p1")
    async def _all():
        return [poulet]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    cases = [_case(1, "midi", poulet, persons=2)]
    r = _enregistrer_brouillon(client, cases)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/?draft=") and "saved=1" in loc
    pid = int(loc.split("draft=")[1].split("&")[0])
    # brouillon (valide=0) : pas dans les validées, présent dans list_drafts
    assert asyncio.run(main.db.list_plannings()) == []
    drafts = asyncio.run(main.db.list_drafts())
    assert [d["id"] for d in drafts] == [pid]
    # data["builder"] contient la grille brute soumise
    data = json.loads(asyncio.run(main.db.get_planning_with_recipes(pid))["data_json"])
    assert data["builder"] == cases


def test_reouvrir_brouillon_injecte_builder(client, monkeypatch):
    """GET /?draft=<id> hydrate la page : INITIAL_BUILDER non vide + draft_id."""
    poulet = _recipe("Poulet", id="p1")
    async def _all():
        return [poulet]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    cases = [_case(1, "midi", poulet, persons=2)]
    r = _enregistrer_brouillon(client, cases)
    pid = int(r.headers["location"].split("draft=")[1].split("&")[0])
    html = client.get(f"/?draft={pid}").text
    assert "Poulet" in html                       # le nom du plat est dans le builder
    assert f'value="{pid}"' in html               # draft_id pré-rempli
    assert '"builder": []' not in html            # builder hydraté (non vide, #page-data)
    # brouillon inconnu / validé : grille vierge
    html2 = client.get("/?draft=99999").text
    assert '"builder": []' in html2


def test_maj_brouillon_ne_cree_pas_de_planning(client, monkeypatch):
    """draft_id fourni → met à jour le brouillon existant, aucun nouveau créé."""
    poulet, soupe = _recipe("Poulet", id="p1"), _recipe("Soupe", id="s1")
    async def _all():
        return [poulet, soupe]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    r = _enregistrer_brouillon(client, [_case(1, "midi", poulet, persons=2)])
    pid = int(r.headers["location"].split("draft=")[1].split("&")[0])
    # ré-enregistrement avec draft_id : même planning mis à jour
    cases2 = [_case(1, "midi", soupe, persons=3)]
    r2 = _enregistrer_brouillon(client, cases2, draft_id=str(pid))
    pid2 = int(r2.headers["location"].split("draft=")[1].split("&")[0])
    assert pid2 == pid
    drafts = asyncio.run(main.db.list_drafts())
    assert len(drafts) == 1                        # pas de nouveau planning
    p = asyncio.run(main.db.get_planning_with_recipes(pid))
    assert {rec["recipe_name"] for rec in p["recipes"]} == {"Soupe"}


def test_brouillons_multiples_coexistent(client, monkeypatch):
    """Deux brouillons enregistrés coexistent (pas de purge en construire)."""
    poulet = _recipe("Poulet", id="p1")
    async def _all():
        return [poulet]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    _enregistrer_brouillon(client, [_case(1, "midi", poulet, persons=2)])
    _enregistrer_brouillon(client, [_case(2, "midi", poulet, persons=2)])
    assert len(asyncio.run(main.db.list_drafts())) == 2


def test_historique_affiche_brouillons_et_suppression(client, monkeypatch):
    """L'historique liste la section Brouillons ; la suppression retire le
    brouillon via /planning/{id}/supprimer."""
    poulet = _recipe("Poulet", id="p1")
    async def _all():
        return [poulet]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    r = _enregistrer_brouillon(client, [_case(1, "midi", poulet, persons=2)])
    pid = int(r.headers["location"].split("draft=")[1].split("&")[0])
    hist = client.get("/historique").text
    assert "📝 Brouillons" in hist
    assert f"/?draft={pid}" in hist                # lien Reprendre
    # suppression
    assert client.post(f"/planning/{pid}/supprimer").json().get("success")
    assert asyncio.run(main.db.list_drafts()) == []
    assert client.post(f"/planning/{pid}/supprimer").json().get("error")


def test_planning_apercu(client, monkeypatch):
    pid = _make_shopping_planning(client, monkeypatch)
    d = client.get(f"/api/planning/{pid}/apercu").json()
    jours = d["jours"]
    assert jours and jours[0]["jour"] == "Lun"
    assert "Poulet" in jours[0]["midi"] and "Soupe" in jours[0]["soir"]
    assert "error" in client.get("/api/planning/999999/apercu").json()


def test_enrich_week(client, monkeypatch):
    pid = _make_shopping_planning(client, monkeypatch)
    d = client.post(f"/api/enrich-week/{pid}").json()
    # 2 recettes du planning (Poulet, Soupe), enrichies depuis le cache
    assert d.get("success") and d["total"] == 2 and d["enriched"] == 2
    # planning introuvable
    assert "error" in client.post("/api/enrich-week/999999").json()


# ── Accompagnements MULTIPLES ──────────────────────────────────────

def test_legacy_accompagnement_read_as_list(client, monkeypatch):
    """Ancien planning stockant `accompagnement` (dict) : lu comme liste à 1
    (affichage + helper) et inclus dans les courses via plat_accompagnements."""
    legacy = {"jour": 1, "moment": "midi", "nom_recette": "Poulet", "notion_id": "p1",
              "accompagnement": {"nom_recette": "Haricots", "notion_id": "h1",
                                 "url": "", "notion_url": ""}}
    # Helper tolérant : le dict singulier devient une liste à 1.
    assert len(planning_svc.plat_accompagnements(legacy)) == 1
    assert planning_svc.plat_accompagnements(legacy)[0]["nom_recette"] == "Haricots"
    # accompagnement absent / None -> liste vide.
    assert planning_svc.plat_accompagnements({"nom_recette": "X"}) == []
    assert planning_svc.plat_accompagnements({"accompagnement": None}) == []

    data = {"plats": [legacy], "liste_courses": [], "per_day": "4,4,4,4,4,4,4"}
    pid = asyncio.run(main.db.save_planning(
        week_start="2026-01-05", saison="Hiver", nb_personnes=4, ingredients_force="",
        data_json=json.dumps(data, ensure_ascii=False),
        recipes=[{"notion_id": "p1", "recipe_name": "Poulet", "repas_type": "Plat",
                  "jour": 1, "moment": "midi"}],
    ))
    # L'accompagnement legacy est bien rendu dans la page.
    assert "Haricots" in client.get(f"/planning/{pid}").text


def test_dupliquer_planning(client, monkeypatch):
    # Crée un planning, le duplique -> nouveau brouillon avec plats copiés
    poulet = _recipe("Poulet", id="p1")
    async def _all():
        return [poulet]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    pid = _construire(client, [_case(1, "midi", poulet, persons=2)])
    d = client.post(f"/planning/{pid}/dupliquer").json()
    assert d["success"] and d["planning_id"] != pid
    # la copie est un brouillon (bandeau de validation présent)
    assert "Valider la semaine" in client.get(f"/planning/{d['planning_id']}").text


def test_planning_warns_non_enrichi(client, monkeypatch):
    import json
    poulet = _recipe("Poulet", id="n1")
    async def _all():
        return [poulet]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    pid = _construire(client, [_case(1, "midi", poulet, persons=2)])
    # recette non enrichie -> avertissement présent
    assert "À compléter" in client.get(f"/planning/{pid}").text
    # après enrichissement (ingrédients en cache) -> plus d'avertissement
    asyncio.run(main.db.save_enriched("n1", "Poulet", ingredients=json.dumps(
        [{"nom": "g de poulet", "quantite": "200", "unite": ""}])))
    assert "À compléter" not in client.get(f"/planning/{pid}").text


def test_planning_repas_string_legacy_rendered_as_one_chip(client, monkeypatch):
    """Ancien planning : « repas » stocké en string → coercé en liste à l'affichage
    (sinon la boucle de chips l'éclaterait lettre par lettre : « P l a t »)."""
    import json
    data = {"plats": [{"jour": 1, "moment": "midi", "nom_recette": "Truc maison",
                       "repas": "Plat", "notion_id": "", "accompagnement": None}],
            "liste_courses": [], "per_day": "4,4,4,4,4,4,4"}
    pid = asyncio.run(main.db.save_planning(
        week_start="2026-01-05", saison="Hiver", nb_personnes=4, ingredients_force="",
        data_json=json.dumps(data, ensure_ascii=False),
        recipes=[{"notion_id": "", "recipe_name": "Truc maison", "repas_type": "Plat",
                  "jour": 1, "moment": "midi"}],
    ))
    html = client.get(f"/planning/{pid}").text
    assert '<span class="chip chip-repas">Plat</span>' in html
    assert '<span class="chip chip-repas">P</span>' not in html


def test_construire_case_absente(client, monkeypatch):
    """Une case à 0 convive (absent) n'est pas générée et le créneau apparaît
    comme désactivé dans la page planning."""
    poulet = _recipe("Poulet", id="p1")
    async def _all():
        return [poulet]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    # midi 1 présent, midi 1 soir absent
    pid = _construire(client, [_case(1, "midi", poulet, persons=2),
                               _case(1, "soir", poulet, persons=0)])
    plats = json.loads(asyncio.run(main.db.get_planning_with_recipes(pid))["data_json"])["plats"]
    assert {(p["jour"], p["moment"]) for p in plats} == {(1, "midi")}
    page = client.get(f"/planning/{pid}").text
    assert "Absent" in page  # placeholder rendu pour le créneau off


def test_week_nutrition(client):
    import json
    async def run():
        await main.db.save_enriched("n1", "Pâtes", ingredients=json.dumps([
            {"nom": "g de pâtes", "quantite": "200", "unite": ""}]))
        plats = [{"jour": j, "moment": m, "nom_recette": "Pâtes", "notion_id": "n1",
                  "accompagnement": None} for j in range(1, 8) for m in ("midi", "soir")]
        nut = await nutrition_week._week_nutrition(plats)
        assert nut and nut["calories"] > 0 and nut["meals_total"] == 14
        # détail par jour : 7 jours, chacun avec midi+soir+total estimés
        assert len(nut["par_jour"]) == 7
        d0 = nut["par_jour"][0]
        assert d0["nom"] == "Lundi" and d0["midi"] and d0["soir"]
        assert d0["total"]["calories"] == d0["midi"]["calories"] + d0["soir"]["calories"]
        # aucune recette estimable -> None
        assert await nutrition_week._week_nutrition([{"jour": 1, "moment": "midi", "nom_recette": "X",
                                            "notion_id": "zzz", "accompagnement": None}]) is None
    asyncio.run(run())


def test_week_label_range():
    # Plage de jours réellement remplis (2026-07-13 = lundi).
    assert planning_svc.week_label("2026-07-13", [{"jour": 2}, {"jour": 5}]) == "Mardi 14/07 → vendredi 17/07"
    assert planning_svc.week_label("2026-07-13", [{"jour": 3}]) == "Mercredi 15/07"
    assert planning_svc.week_label("2026-07-13", []) == "Semaine du 2026-07-13"
    assert planning_svc.week_label("bad", [{"jour": 1}]) == "Semaine du bad"


def test_week_label_non_monday():
    # week_start = date du JOUR 1 (mardi 14/07/2026), pas forcément un lundi.
    # Le nom du jour vient de la VRAIE date, pas de l'index.
    assert planning_svc.week_label("2026-07-14", [{"jour": 1}]) == "Mardi 14/07"
    assert planning_svc.week_label("2026-07-14", [{"jour": 1}, {"jour": 3}]) == "Mardi 14/07 → jeudi 16/07"


def test_day_labels():
    # jour 1..7 dérivés d'une date de départ arbitraire (mardi 14/07/2026).
    dl = planning_svc.day_labels("2026-07-14")
    assert len(dl) == 7
    assert dl[0] == {"jour": 1, "court": "Mar", "long": "Mardi", "date": "14/07"}
    assert dl[3] == {"jour": 4, "court": "Ven", "long": "Vendredi", "date": "17/07"}
    # jour 7 = +6j = lundi 20/07.
    assert dl[6]["long"] == "Lundi" and dl[6]["date"] == "20/07"
    # Repli neutre si date invalide.
    bad = planning_svc.day_labels("bad")
    assert bad[0] == {"jour": 1, "court": "J1", "long": "Jour 1", "date": ""}


def test_planning_apercu_non_monday(client, monkeypatch):
    # L'aperçu doit refléter le VRAI jour de semaine du jour 1 (mardi), pas « Lun ».
    poulet = _recipe("Poulet rôti")

    async def _all():
        return [poulet]
    monkeypatch.setattr(main.notion, "get_all_recipes", _all)
    pid = _construire(client, [_case(1, "midi", poulet, persons=2)],
                      week_start="2026-07-14")
    d = client.get(f"/api/planning/{pid}/apercu").json()
    assert d["jours"] and d["jours"][0]["jour"] == "Mar"
