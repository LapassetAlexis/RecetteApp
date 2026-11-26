"""Fonctions pures du constructeur (`window.Builder`), exécutées dans la page
« / » par `page.evaluate` : groupes de midis, sérialisation, période, copie,
restes, filtres du picker."""

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.e2e


@pytest.fixture
def B(page: Page, server):
    page.goto(server + "/")
    page.wait_for_function("() => window.Builder && window.BuilderPicker")

    def run(corps: str):
        return page.evaluate(f"() => {{ const B = window.Builder; {corps} }}")
    return run


def test_groupes_de_midis(B):
    r = B("""
        const s = B.initialState(), all = B.activeDaysFor(7);
        const meal = n => ({notion_id: n, nom: n, nature: 'Recette'});
        // Repas posé sur un suiveur (mardi, groupe 1) → remonté sur lundi.
        s['2:midi'].main = meal('Gratin');
        B.consolidateGroups(s, all);
        const apres = [s['1:midi'].main && s['1:midi'].main.nom, s['2:midi'].main];
        // Lundi absent → mardi devient l'éditeur du groupe 1.
        s['1:midi'].persons = 0;
        return {apres, editeur: B.midiEditorDay(s, all, 1), groupes: [1,2,3,4,5,6,7].map(d => s[d + ':midi'].group)};
    """)
    assert r["apres"] == ["Gratin", None]
    assert r["editeur"] == 2
    assert r["groupes"] == [1, 1, 2, 2, 2, 3, 4]


def test_serialisation_et_reouverture(B):
    r = B("""
        const s = B.initialState(), days = B.activeDaysFor(3);
        s['1:midi'].main = {notion_id: 'p1', nom: 'Poulet', nature: 'Recette'};
        s['1:midi'].accompagnements = [{notion_id: '', nom: 'Salade', nature: 'Libre', libre: true}];
        s['3:soir'].main = {notion_id: 'c1', nom: 'Curry', nature: 'Recette'};
        s['5:soir'].main = {notion_id: 'g1', nom: 'Gratin', nature: 'Recette'};   // hors période
        const ser = B.serialize(s, days);
        const re = B.hydrateBuilder(B.initialState(), ser);
        return {
            n: ser.length,
            hors: ser.filter(c => c.jour > 3).map(c => [c.persons, !!c.main, c.accompagnements.length]),
            midi1: ser.find(c => c.jour === 1 && c.moment === 'midi'),
            soirSansGroupe: !('group' in ser.find(c => c.jour === 1 && c.moment === 'soir')),
            nbJours: B.nbDaysInitial(ser),
            reMain: re['3:soir'].main.nom, reSide: re['1:midi'].accompagnements[0],
            gardeEnMemoire: s['5:soir'].main.nom,
        };
    """)
    assert r["n"] == 14
    assert all(h == [0, False, 0] for h in r["hors"])        # jours hors période : absents, sans repas
    assert r["midi1"]["group"] == 1 and r["midi1"]["main"]["notion_id"] == "p1"
    assert r["midi1"]["accompagnements"] == [{"notion_id": "", "nom": "Salade", "nature": "Libre", "libre": True}]
    assert r["soirSansGroupe"]
    assert r["nbJours"] == 3
    assert r["reMain"] == "Curry"
    assert r["reSide"]["libre"] is True
    assert r["gardeEnMemoire"] == "Gratin"                   # re-sélectionner le jour restaure le repas


def test_periode(B):
    r = B("""
        const d = B.parseDate;
        return {
            quatre: B.rangeDays(d('2026-11-25'), d('2026-11-28'), 7).n,
            inverse: B.rangeDays(d('2026-11-28'), d('2026-11-25'), 7).n,
            trop: B.rangeDays(d('2026-11-01'), d('2026-11-20'), 7).n,
            sansFin: B.rangeDays(d('2026-11-01'), null, 5),
            jour3: B.dayInfo(d('2026-11-25'), 3),
            vide: B.nbDaysInitial([]),
            invalide: B.parseDate('25/11/2026'),
        };
    """)
    assert (r["quatre"], r["inverse"], r["trop"]) == (4, 1, 7)
    assert r["sansFin"] == {"n": 5, "hint": None}
    assert r["jour3"] == {"court": "Ven", "long": "Vendredi", "date": "27/11"}
    assert r["vide"] == 7
    assert r["invalide"] is None


def test_copier_deplacer_restes(B):
    r = B("""
        const s = B.initialState(), all = B.activeDaysFor(7);
        const meal = n => ({notion_id: n, nom: n, nature: 'Recette'});
        s['1:midi'].main = meal('Lundi'); s['1:soir'].main = meal('Curry');
        s['1:soir'].accompagnements = [meal('Riz')];
        const cible = B.leftoversTarget(s, all, '1:soir');
        B.applyLeftovers(s, all, '1:soir'); B.consolidateGroups(s, all);
        const restes = [s['2:midi'].main.nom, s['1:midi'].main.nom, s['2:midi'].group !== s['1:midi'].group];
        s['2:midi'].accompagnements.push(meal('X'));            // copies indépendantes
        const independant = s['1:soir'].accompagnements.length === 1;
        const copie = B.copyMeal(s, all, '1:soir', '4:soir');
        const deplace = B.moveMeal(s, all, '4:soir', '6:soir');
        return {cible, restes, independant, copie, deplace,
                quatre: s['4:soir'].main, six: s['6:soir'].main.nom,
                dimanche: (s['7:soir'].main = meal('D'), B.leftoversTarget(s, all, '7:soir'))};
    """)
    assert r["cible"] == "2:midi"
    assert r["restes"] == ["Curry", "Lundi", True]
    assert r["independant"]
    assert r["copie"] and r["deplace"]
    assert r["quatre"] is None and r["six"] == "Curry"
    assert r["dimanche"] is None


def test_filtres_du_picker_et_nom_piege(B, page: Page):
    page.wait_for_function("() => fetch('/api/catalogue').then(r => r.ok)")
    r = page.evaluate("""async () => {
        const B = window.Builder;
        const cat = await (await fetch('/api/catalogue')).json();
        const noms = o => B.sortCatalogue(B.filterCatalogue(cat, o)).map(x => x.nom);
        return {
            total: cat.length,
            plats: noms({mode: 'main'}),
            accomp: noms({mode: 'side'}),
            cesar: noms({mode: 'main', q: '"césar" <b>'}),
            rapide: noms({mode: 'main', extra: new Set(['rapide'])}),
            briques: noms({mode: 'main', nature: 'Ingrédient'}),
            esc: window.Common.esc('Salade "César" <b>x</b>'),
        };
    }""")
    assert r["total"] == 10
    assert "Tarte aux pommes" not in r["plats"] and len(r["plats"]) == 9
    assert sorted(r["accomp"]) == ["Haricots verts sautés", "Riz", "Soupe de potiron"]
    assert r["cesar"] == ['Salade "César" <b>x</b>']
    assert r["rapide"] == ["Poulet rôti"]
    assert r["briques"] == ["Riz"]
    assert r["esc"] == "Salade &quot;César&quot; &lt;b&gt;x&lt;/b&gt;"
