"""Connexion par session : formulaire de login et déconnexion."""

import secrets
import time
from collections import deque

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings
from app.deps import templates

router = APIRouter()

# Limite des échecs de connexion, en mémoire (un seul processus uvicorn) :
# au-delà de LOGIN_LIMIT échecs en LOGIN_WINDOW secondes, une adresse reçoit
# 429 sans que les identifiants soient vérifiés.
LOGIN_LIMIT = 5
LOGIN_WINDOW = 60.0
_echecs: dict[str, deque] = {}


def _adresse(request: Request) -> str:
    return request.client.host if request.client else "?"


def _echecs_recents(adresse: str, now: float) -> deque:
    hits = _echecs.setdefault(adresse, deque())
    while hits and now - hits[0] >= LOGIN_WINDOW:
        hits.popleft()
    return hits


def _bloque(adresse: str, now: float | None = None) -> bool:
    """True si `adresse` a déjà LOGIN_LIMIT échecs dans la fenêtre."""
    now = time.monotonic() if now is None else now
    return len(_echecs_recents(adresse, now)) >= LOGIN_LIMIT


def _noter_echec(adresse: str, now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    _echecs_recents(adresse, now).append(now)
    # Adresses inactives retirées : la table ne grossit pas sans fin.
    if len(_echecs) > 1000:
        for a in [a for a, h in _echecs.items() if not h or now - h[-1] >= LOGIN_WINDOW]:
            _echecs.pop(a, None)


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
    adresse = _adresse(request)
    if _bloque(adresse):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": next, "error": "Trop de tentatives : réessaie dans une minute."},
            status_code=429,
        )
    # Comparaison en octets UTF-8 : compare_digest refuse les str non ASCII
    # (un accent saisi provoquait une 500 au lieu d'un refus).
    ok = (secrets.compare_digest(username.encode("utf-8"), settings.auth_user.encode("utf-8"))
          and secrets.compare_digest(password.encode("utf-8"), settings.auth_password.encode("utf-8")))
    if not ok:
        _noter_echec(adresse)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": next, "error": "Identifiants incorrects."},
            status_code=401,
        )
    request.session["authed"] = True
    return RedirectResponse(url=next, status_code=303)


@router.post("/logout")
async def logout(request: Request):
    """Déconnexion (POST : un lien ou un préchargement ne déconnecte pas)."""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
