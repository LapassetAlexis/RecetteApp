"""M01 — socle front : lecture des fichiers statiques (le comportement est
couvert par `tests/e2e/test_parcours_articles.py`)."""

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
COMMON = (APP / "static" / "js" / "common.js").read_text(encoding="utf-8")
BASE = (APP / "templates" / "base.html").read_text(encoding="utf-8")


def _exports() -> set[str]:
    blocs = re.findall(r"window\.Common\s*=\s*\{([^}]*)\}", COMMON)
    blocs += re.findall(r"Object\.assign\(window\.Common,\s*\{([^}]*)\}\)", COMMON)
    assert blocs, "window.Common introuvable"
    return {n.strip() for b in blocs for n in b.split(",") if n.strip()}


def test_common_expose_modal_et_requestjson():
    assert {"modal", "requestJson", "postJson", "esc", "pageData", "qtyFr", "copyText"} <= _exports()
    assert "function modal(el, opts)" in COMMON
    assert "async function requestJson(url, opts)" in COMMON


def test_postjson_passe_par_requestjson_et_lit_le_statut():
    assert "requestJson(url, {method: 'POST', body})" in COMMON
    assert "r.status === 401" in COMMON
    assert "r.redirected" in COMMON
    assert "/login?next=" in COMMON
    assert "Réponse inattendue du serveur" in COMMON


def test_ecouteur_submit_global_en_remontee_et_respecte_preventdefault():
    debut = BASE.index("document.addEventListener('submit'")
    bloc = BASE[debut:BASE.index("window.addEventListener('pageshow'", debut)]
    assert "e.defaultPrevented" in bloc
    assert ", true)" not in bloc, "l'écouteur ne doit plus être en phase de capture"
