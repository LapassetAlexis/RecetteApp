"""Pages générales : accueil (constructeur), historique, service worker, santé."""

import json
from datetime import date, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse

from app.config import settings
from app.deps import BASE_DIR, db, templates
from app.services.planning import _attach_label, _builder_from_plats

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, draft: str = "", saved: str = ""):
    """Page d'accueil : constructeur manuel de planning (grille 7 jours × 2).

    `?draft=<id>` rouvre un brouillon existant : on ré-injecte la grille du
    formulaire (stockée dans `data["builder"]`) pour l'éditer puis la mettre à
    jour. `?saved=1` affiche un message « brouillon enregistré »."""
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
    if draft:
        try:
            did = int(draft)
        except (TypeError, ValueError):
            did = 0
        if did:
            planning = await db.get_planning_with_recipes(did)
            # Uniquement les brouillons (valide=0) sont ré-éditables.
            if planning and not planning.get("valide", 0):
                try:
                    data = json.loads(planning["data_json"])
                    b = data.get("builder")
                    if not isinstance(b, list):
                        # Ancien brouillon sans grille : reconstruite depuis les plats.
                        b = _builder_from_plats(
                            data.get("plats", []), data.get("off_meals"),
                            planning.get("nb_personnes") or 4,
                        )
                    if isinstance(b, list):
                        builder = b
                        draft_id = str(did)
                        week_start = planning.get("week_start") or week_start
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
            "saved": bool(saved),
        },
    )


@router.get("/historique", response_class=HTMLResponse)
async def historique(request: Request):
    """Liste les plannings précédents (validés) et les brouillons en cours."""
    plannings = [_attach_label(p) for p in await db.list_plannings(limit=20)]
    drafts = [_attach_label(p) for p in await db.list_drafts(limit=20)]
    return templates.TemplateResponse(
        request,
        "historique.html",
        {"plannings": plannings, "drafts": drafts},
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
