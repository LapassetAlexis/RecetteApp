"""Page « Données » : export, import (avec récapitulatif) et réhydratation
du cache d'ingrédients depuis Notion.

Protégée par l'auth de session quand elle est active (`/admin` n'est pas dans
les préfixes publics de `app.main`)."""

import json
import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from app import deps
from app.services import backup

logger = logging.getLogger(__name__)
router = APIRouter()

# Imports en attente de confirmation : {jeton: (horodatage, données validées, mode)}.
# En mémoire (un seul processus) : un redémarrage entre les deux étapes oblige
# simplement à renvoyer le fichier.
_PENDING: dict[str, tuple[float, dict[str, Any], str]] = {}
_PENDING_TTL = 15 * 60
_PENDING_MAX = 5

MAX_UPLOAD = 50 * 1024 * 1024  # 50 Mo : très au-delà d'une base de foyer


def _purge_pending() -> None:
    now = time.monotonic()
    for k in [k for k, (t, _, _) in _PENDING.items() if now - t > _PENDING_TTL]:
        _PENDING.pop(k, None)
    while len(_PENDING) > _PENDING_MAX:
        _PENDING.pop(next(iter(_PENDING)))


async def _render(request: Request, status_code: int = 200, **ctx: Any) -> HTMLResponse:
    ctx.setdefault("counts", await deps.db.counts())
    return deps.templates.TemplateResponse(request, "admin.html", ctx, status_code=status_code)


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """Page « Données » : état de la base et actions de sauvegarde."""
    return await _render(request)


@router.get("/admin/export")
async def admin_export():
    """Téléchargement de toutes les données locales en JSON."""
    data = await backup.export_all(deps.db)
    return Response(
        json.dumps(data, ensure_ascii=False, indent=1),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{backup.export_filename()}"'},
    )


@router.post("/admin/import", response_class=HTMLResponse)
async def admin_import(
    request: Request,
    fichier: UploadFile | None = File(None),
    mode: str = Form("merge"),
    jeton: str = Form(""),
):
    """Import en deux temps.

    1. Envoi du fichier (+ mode) : validation, puis récapitulatif à confirmer ;
       rien n'est écrit.
    2. Renvoi du `jeton` reçu : application de l'import en une transaction."""
    if mode not in backup.MODES:
        return await _render(request, 400, error=f"Mode inconnu : {mode}")

    if jeton:
        _purge_pending()
        pending = _PENDING.pop(jeton, None)
        if not pending:
            return await _render(request, 400, error="Import expiré ou déjà appliqué : renvoyez le fichier.")
        _, data, pending_mode = pending
        try:
            added = await backup.import_all(deps.db, data, pending_mode)
        except backup.BackupError as e:
            return await _render(request, 400, error=str(e))
        logger.info("Import %s appliqué : %s", pending_mode, added)
        return await _render(request, done={"mode": pending_mode, **added})

    if fichier is None or not fichier.filename:
        return await _render(request, 400, error="Choisissez un fichier de sauvegarde.")
    raw = await fichier.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        return await _render(request, 400, error="Fichier trop volumineux.")
    try:
        data = backup.parse_backup(raw)
    except backup.BackupError as e:
        return await _render(request, 400, error=f"Fichier refusé, rien n'a été modifié : {e}")

    _purge_pending()
    token = secrets.token_urlsafe(16)
    _PENDING[token] = (time.monotonic(), data, mode)
    return await _render(
        request, recap=backup.summarize(data), recap_mode=mode, jeton=token,
        nom_fichier=fichier.filename,
    )


@router.post("/admin/rehydrater", response_class=HTMLResponse)
async def admin_rehydrater(request: Request):
    """Reconstruit les ingrédients manquants du cache depuis Notion."""
    try:
        stats = await backup.rehydrate_from_notion(deps.db, deps.notion)
    except Exception:
        logger.exception("Réhydratation depuis Notion impossible")
        return await _render(request, 502, error="Notion est injoignable : réessayez plus tard.")
    return await _render(request, rehydrated=stats)
