"""Pages générales : accueil (constructeur), historique, service worker, santé."""

import json
from datetime import date, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse

from app.config import settings
from app.deps import BASE_DIR, db, templates
from app.services.planning import _attach_label, _builder_from_plats, _nb_jours

router = APIRouter()


def _parse_id(raw: str) -> int:
    try:
        return int(raw) if raw else 0
    except (TypeError, ValueError):
        return 0


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, draft: str = "", planning: str = "", saved: str = ""):
    """Page d'accueil : constructeur manuel de planning (grille 7 jours × 2).

    `?draft=<id>` rouvre un brouillon existant : on ré-injecte la grille du
    formulaire (stockée dans `data["builder"]`) pour l'éditer puis la mettre à
    jour. `?planning=<id>` fait de même pour N'IMPORTE QUEL planning, validé
    ou non (« ✏️ Modifier dans le constructeur ») : à l'enregistrement, une
    semaine validée le reste. `?saved=1` affiche un message « enregistré »
    (« Semaine mise à jour » pour une semaine validée)."""
    today = date.today()
    # Calcul du lundi de la semaine
    lundi = today - timedelta(days=today.weekday())
    week_start = lundi.isoformat()

    # Dernier planning (lien « Voir »)
    dernier = await db.get_last_planning()
    planning_id = dernier["id"] if dernier else None

    # Réouverture d'un brouillon : on hydrate la grille depuis data["builder"].
    builder: list = []
    draft_id = ""
    nb_jours = 0
    valide = False
    # ?planning= : tout planning ; ?draft= : uniquement un brouillon (valide=0).
    did = _parse_id(planning) or _parse_id(draft)
    if did:
        existing = await db.get_planning_with_recipes(did)
        if existing and (_parse_id(planning) or not existing.get("valide", 0)):
            try:
                data = json.loads(existing["data_json"])
                b = data.get("builder")
                if not isinstance(b, list):
                    # Ancien planning sans grille : reconstruite depuis les plats.
                    b = _builder_from_plats(
                        data.get("plats", []), data.get("off_meals"),
                        existing.get("nb_personnes") or 4,
                    )
                if isinstance(b, list):
                    builder = b
                    draft_id = str(did)
                    week_start = existing.get("week_start") or week_start
                    valide = bool(existing.get("valide", 0))
                    # Période enregistrée (un dernier jour absent en fait partie).
                    if data.get("nb_jours"):
                        nb_jours = _nb_jours(data)
            except (json.JSONDecodeError, TypeError):
                pass

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "week_start": week_start,
            "planning_id": planning_id,
            "builder": builder,
            "draft_id": draft_id,
            "nb_jours": nb_jours,
            "valide": valide,
            "saved": bool(saved),
        },
    )


HISTORIQUE_PAGE = 20


@router.get("/historique", response_class=HTMLResponse)
async def historique(request: Request, n: int = HISTORIQUE_PAGE):
    """Liste les plannings précédents (validés) et les brouillons en cours.

    `?n=<nombre>` : nombre de semaines validées affichées (« Voir plus » ajoute
    une page de 20). On en lit une de plus pour savoir s'il en reste."""
    n = max(HISTORIQUE_PAGE, min(n, 1000))
    lus = await db.list_plannings(limit=n + 1)
    plannings = [_attach_label(p) for p in lus[:n]]
    drafts = [_attach_label(p) for p in await db.list_drafts(limit=20)]
    return templates.TemplateResponse(
        request,
        "historique.html",
        {"plannings": plannings, "drafts": drafts,
         "next_n": n + HISTORIQUE_PAGE if len(lus) > n else 0},
    )


@router.get("/sw.js")
async def service_worker():
    """Sert le service worker depuis la racine (scope = tout le site)."""
    return FileResponse(
        str(BASE_DIR / "static" / "sw.js"),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@router.get("/health")
async def health():
    return {"status": "ok", "app": settings.app_title}
