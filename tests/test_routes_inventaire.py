"""Inventaire figé des routes : le découpage de main.py en routeurs ne doit
ajouter, perdre ni déplacer aucune route (méthode, chemin)."""

from starlette.routing import Mount, Route

import app.main as main

ROUTES_ATTENDUES = {
    ("GET", "/"),
    ("GET", "/admin"),
    ("GET", "/admin/export"),
    ("GET", "/ajouter"),
    ("GET", "/api/alternatives/{planning_id}"),
    ("GET", "/api/catalogue"),
    ("GET", "/api/enrich-all/stream"),
    ("GET", "/api/planning/{planning_id}/apercu"),
    ("POST", "/api/planning/{planning_id}/meals"),
    ("GET", "/courses/{token}"),
    ("GET", "/courses/{token}/etat"),
    ("POST", "/courses/{token}/check"),
    ("POST", "/api/courses/{planning_id}/extra"),
    ("DELETE", "/api/courses/{planning_id}/extra/{key}"),
    ("POST", "/api/courses/{planning_id}/masquer"),
    ("POST", "/api/courses/{planning_id}/placard"),
    ("POST", "/api/courses/{planning_id}/extra/{key}/rayon"),
    ("GET", "/api/courses/{planning_id}/etat"),
    ("GET", "/api/placard"),
    ("DELETE", "/api/placard/{nom}"),
    ("GET", "/docs"),
    ("GET", "/docs/oauth2-redirect"),
    ("GET", "/health"),
    ("GET", "/historique"),
    ("GET", "/login"),
    ("GET", "/logout"),
    ("GET", "/openapi.json"),
    ("GET", "/planning/{planning_id}"),
    ("GET", "/recette/{page_id}"),
    ("GET", "/recette/{page_id}/enrichir"),
    ("GET", "/recette/{page_id}/cuisine"),
    ("GET", "/recettes"),
    ("GET", "/redoc"),
    ("GET", "/sw.js"),
    ("HEAD", "/docs"),
    ("HEAD", "/docs/oauth2-redirect"),
    ("HEAD", "/openapi.json"),
    ("HEAD", "/redoc"),
    ("MOUNT", "/static"),
    ("POST", "/admin/import"),
    ("POST", "/admin/rehydrater"),
    ("POST", "/ajouter-recette"),
    ("POST", "/api/analyze-text"),
    ("POST", "/api/analyze-url"),
    ("POST", "/api/brique/{planning_id}"),
    ("POST", "/api/enrich-week/{planning_id}"),
    ("POST", "/api/free-meal/{planning_id}"),
    ("POST", "/api/rate/{page_id}"),
    ("POST", "/api/recette/{page_id}/planifier"),
    ("POST", "/api/shopping-check/{planning_id}"),
    ("POST", "/construire"),
    ("POST", "/generate-shopping/{planning_id}"),
    ("POST", "/login"),
    ("POST", "/planning/{planning_id}/dupliquer"),
    ("POST", "/planning/{planning_id}/partager"),
    ("POST", "/planning/{planning_id}/supprimer"),
    ("POST", "/planning/{planning_id}/valider"),
    ("POST", "/recette/{page_id}/enrichir"),
    ("POST", "/recette/{page_id}/supprimer"),
}

# Routes sans appelant dans l'interface, supprimées au découpage.
ROUTES_SUPPRIMEES = {
    ("POST", "/api/enrich/{page_id}"),
    ("POST", "/api/enrich-all"),
    ("POST", "/api/update-side/{planning_id}"),
}


def _inventaire() -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for r in main.app.routes:
        if isinstance(r, Route):
            out.update((m, r.path) for m in r.methods or ())
        elif isinstance(r, Mount):
            out.add(("MOUNT", r.path))
    return out


def test_inventaire_des_routes_fige():
    assert _inventaire() == ROUTES_ATTENDUES


def test_routes_mortes_supprimees():
    assert not (_inventaire() & ROUTES_SUPPRIMEES)


def test_aucune_route_en_double():
    couples = [(m, r.path) for r in main.app.routes if isinstance(r, Route) for m in r.methods or ()]
    assert len(couples) == len(set(couples))
