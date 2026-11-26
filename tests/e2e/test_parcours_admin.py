"""Page /admin au clic (M11) : export téléchargé, puis import « remplacer » en
deux temps (récapitulatif, confirmation) qui restaure la base exportée.

Serveur isolé (`server_isole`) : l'import remplace toute la base."""

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.outils import pas_de_defilement_horizontal, semaine, semaine_validee

pytestmark = pytest.mark.e2e


def _stats(page: Page) -> list[str]:
    return page.locator(".admin-stats li strong").all_inner_texts()


@pytest.mark.parametrize("largeur", [1280, 390])
def test_export_puis_import_remplacement(page: Page, server_isole, tmp_path, largeur):
    page.set_viewport_size({"width": largeur, "height": 900 if largeur > 500 else 844})
    server = server_isole
    semaine_validee(page, server, semaine(2, {"1:midi": "p1", "2:soir": "c1"}), "2026-11-02")
    semaine_validee(page, server, semaine(2, {"1:midi": "g1"}), "2026-11-09")

    page.goto(server + "/admin")
    expect(page.locator(".alert").filter(has_text="Accès non protégé")).to_be_visible()   # D6
    assert _stats(page)[0] == "2"
    pas_de_defilement_horizontal(page)

    # Export : un fichier JSON téléchargé au clic.
    with page.expect_download() as dl:
        page.get_by_role("link", name="⬇️ Télécharger la sauvegarde").click()
    fichier = tmp_path / dl.value.suggested_filename
    dl.value.save_as(fichier)
    assert re.fullmatch(r"menu-planner-\d{4}-\d{2}-\d{2}\.json", fichier.name)
    assert len(json.loads(fichier.read_text(encoding="utf-8"))["plannings"]) == 2

    # Une semaine de plus après l'export : le remplacement doit la retirer.
    semaine_validee(page, server, semaine(1, {"1:soir": "s1"}), "2026-11-16")
    page.goto(server + "/admin")
    assert _stats(page)[0] == "3"

    # Étape 1 : fichier + mode « Remplacer » → récapitulatif, rien n'est modifié.
    page.set_input_files("#fichier", str(fichier))
    page.get_by_label("Remplacer : effacer la base puis restaurer").check()
    page.get_by_role("button", name="Vérifier le fichier").click()
    recap = page.locator(".admin-recap")
    expect(recap).to_be_visible()
    expect(recap.locator("tbody tr").first.locator("td")).to_have_text(["2", "3"])   # fichier, base
    expect(recap.locator(".alert-warn")).to_contain_text("Mode remplacement")
    assert _stats(page)[0] == "3"
    pas_de_defilement_horizontal(page)

    # Étape 2 : confirmation → base remplacée par le fichier.
    page.get_by_role("button", name="Confirmer l'import").click()
    expect(page.locator(".alert-success")).to_contain_text("Import en remplacement appliqué : 2 planning(s)")
    assert _stats(page)[0] == "2"
    page.goto(server + "/historique")
    expect(page.locator(".hist-item")).to_have_count(2)
