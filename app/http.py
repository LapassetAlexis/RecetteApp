"""Réponses d'erreur et lecture du corps JSON, communes à tous les routeurs.

Convention (décision D4) : une erreur répond par un statut HTTP 4xx / 5xx et
un corps `{"error": "message lisible", "code"?: "..."}`. Jamais de 200 avec
`error`, jamais le texte d'une exception (il va dans les logs) :

- 400 : corps ou paramètres invalides ;
- 404 : planning, recette ou article introuvable ;
- 413 : corps trop volumineux ;
- 429 : trop de requêtes ;
- 502 : Notion ou le LLM en échec ;
- 500 : erreur interne, message générique."""

import json
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

MAX_BODY = 64 * 1024  # 64 Ko : très au-delà de tout corps JSON légitime

ERREUR_INTERNE = "Erreur interne, réessaie plus tard."
ERREUR_NOTION = "Notion est injoignable, réessaie plus tard."
ERREUR_LLM = "L'analyse automatique a échoué, réessaie plus tard."


def erreur(message: str, status: int = 400, code: str | None = None) -> JSONResponse:
    """Réponse d'erreur JSON : `{"error": message, "code"?: code}`."""
    body: dict[str, Any] = {"error": message}
    if code:
        body["code"] = code
    return JSONResponse(body, status_code=status)


def statut_metier(message: str) -> int:
    """Statut d'un message d'erreur renvoyé par un service (`{"error": …}`) :
    404 pour un objet introuvable, 502 pour une création Notion impossible,
    400 sinon (paramètre refusé)."""
    m = (message or "").lower()
    if "introuvable" in m or "non trouvé" in m:
        return 404
    if "impossible" in m:
        return 502
    return 400


def externe(exc: BaseException) -> bool:
    """True si l'exception vient d'un service externe (Notion, LLM, site)."""
    # Lecture par le module (et non import du nom) : les tests rechargent
    # `app.llm_client`, la classe change alors d'identité.
    from app import llm_client
    return isinstance(exc, (httpx.HTTPError, llm_client.LLMError))


def erreur_exception(exc: BaseException, message_externe: str = ERREUR_NOTION) -> JSONResponse:
    """Erreur d'une exception déjà journalisée : 502 si un service externe a
    échoué, 500 sinon. Jamais le texte de l'exception."""
    if externe(exc):
        return erreur(message_externe, 502)
    return erreur(ERREUR_INTERNE, 500)


def depuis_service(res: dict, status_echec: int | None = None) -> dict | JSONResponse:
    """Réponse d'un service : telle quelle si elle réussit, sinon une erreur
    au statut `status_echec` (par défaut déduit du message)."""
    if isinstance(res, dict) and res.get("error"):
        msg = str(res["error"])
        return erreur(msg, status_echec or statut_metier(msg))
    return res


class CorpsInvalide(Exception):
    """Corps de requête refusé ; porte la réponse d'erreur à renvoyer."""

    def __init__(self, reponse: JSONResponse):
        super().__init__("corps invalide")
        self.reponse = reponse


async def lire_json(request: Request, code: str | None = None) -> dict:
    """Corps JSON (objet) de la requête, au plus MAX_BODY octets.

    Corps vide → `{}` (les routes valident ensuite les champs). Corps trop
    volumineux → CorpsInvalide (413) ; illisible ou autre chose qu'un objet
    JSON → CorpsInvalide (400). `code` est ajouté aux réponses (routes
    publiques)."""
    taille = request.headers.get("content-length")
    try:
        trop_gros = taille is not None and int(taille) > MAX_BODY
    except ValueError:
        trop_gros = False
    if trop_gros:
        raise CorpsInvalide(erreur("Requête trop volumineuse", 413, code and "taille"))
    brut = bytearray()
    async for morceau in request.stream():
        brut.extend(morceau)
        if len(brut) > MAX_BODY:
            raise CorpsInvalide(erreur("Requête trop volumineuse", 413, code and "taille"))
    if not brut.strip():
        return {}
    try:
        body = json.loads(bytes(brut))
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict):
        raise CorpsInvalide(erreur("Corps JSON attendu", 400, code))
    return body


_PAGE_ERREUR = """{% extends "base.html" %}
{% block title %}{{ titre }}{% endblock %}
{% block content %}
<div class="hero"><h1>{{ titre }}</h1></div>
<div class="alert alert-error" role="alert">{{ message }}</div>
<p><a class="btn btn-secondary" href="{{ retour }}">{{ retour_label }}</a></p>
{% endblock %}"""


def page_erreur(request: Request, templates, message: str, status: int,
                titre: str = "Page indisponible", retour: str = "/historique",
                retour_label: str = "← Historique") -> HTMLResponse:
    """Page HTML d'erreur lisible (gabarit de base, menu compris)."""
    html = templates.env.from_string(_PAGE_ERREUR).render(
        request=request, titre=titre, message=message,
        retour=retour, retour_label=retour_label,
    )
    return HTMLResponse(html, status_code=status)
