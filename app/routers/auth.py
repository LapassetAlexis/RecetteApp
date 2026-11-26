"""Connexion par session : formulaire de login et déconnexion."""

import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings
from app.deps import templates

router = APIRouter()


def _safe_next(url: str) -> str:
    """Cible de redirection après login : uniquement un chemin LOCAL.

    Accepte « /… » mais rejette « //hôte », « /\\hôte », tout schéma
    (« https: », « javascript: ») et les caractères de contrôle → « / »
    (évite une redirection ouverte vers un site externe)."""
    if not url or not isinstance(url, str):
        return "/"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in url) or "\\" in url:
        return "/"
    if not url.startswith("/") or url.startswith("//"):
        return "/"
    return url


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/", error: str = ""):
    """Formulaire de connexion (session). Publique."""
    next = _safe_next(next)
    if not settings.auth_enabled or request.session.get("authed"):
        return RedirectResponse(url=next, status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"next": next, "error": error},
    )


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
):
    """Vérifie les identifiants → ouvre une session persistante (30 j)."""
    next = _safe_next(next)
    # Comparaison en octets UTF-8 : compare_digest refuse les str non ASCII
    # (un accent saisi provoquait une 500 au lieu d'un refus).
    ok = (secrets.compare_digest(username.encode("utf-8"), settings.auth_user.encode("utf-8"))
          and secrets.compare_digest(password.encode("utf-8"), settings.auth_password.encode("utf-8")))
    if not ok:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": next, "error": "Identifiants incorrects."},
            status_code=401,
        )
    request.session["authed"] = True
    return RedirectResponse(url=next, status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
