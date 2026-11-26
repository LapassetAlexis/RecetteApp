"""App FastAPI — Menu Planner : constructeur de semaine, planning, courses et
livre de recettes (Notion). Ce module assemble l'application : cycle de vie,
middlewares (auth par session) et routeurs."""

import logging
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.deps import BASE_DIR, VERSION, db, llm, notion, templates
from app.http import ERREUR_INTERNE, CorpsInvalide, erreur
from app.routers import admin, auth, catalogue, courses, cuisine, pages, planning, recettes

__all__ = ["BASE_DIR", "VERSION", "app", "db", "llm", "notion", "settings", "templates"]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Chemins accessibles sans session quand l'auth est active : sondes et assets,
# service worker (enregistré par toutes les pages, y compris le login), page de
# login, et /courses/<token> (partage public, le token fait office d'accès).
_PUBLIC_PREFIXES = ("/health", "/static", "/sw.js", "/courses/", "/login", "/logout")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    logger.info("Base SQLite initialisée")
    try:
        await notion.ensure_ingredients_field()
    except Exception as e:
        logger.warning(f"Impossible de créer le champ Ingrédients Notion: {e}")
    yield
    await notion.aclose()


app = FastAPI(title=settings.app_title, version=VERSION, lifespan=lifespan)
# Auth par SESSION (formulaire de login), activée seulement si AUTH_USER +
# AUTH_PASSWORD sont définis.
if settings.auth_enabled:

    @app.middleware("http")
    async def session_auth(request: Request, call_next):
        if request.url.path.startswith(_PUBLIC_PREFIXES) or request.session.get("authed"):
            return await call_next(request)
        # API → 401 JSON ; pages → redirection vers le formulaire de login.
        if request.url.path.startswith("/api/"):
            return erreur("Authentification requise", 401, "auth")
        nxt = request.url.path
        if request.url.query:
            nxt += "?" + request.url.query
        return RedirectResponse(url=f"/login?next={quote(nxt, safe='')}", status_code=303)

    logger.info("🔒 Auth par session activée")

# SessionMiddleware ajouté EN DERNIER → couche la plus EXTERNE → `request.session`
# est disponible dans le middleware d'auth ci-dessus. Session persistante (cookie
# signé, 30 j) : plus de re-login à chaque visite, le navigateur peut enregistrer
# le mot de passe.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    max_age=60 * 60 * 24 * 30,          # 30 jours
    same_site="lax",
    https_only=False,                    # derrière un proxy éventuellement en HTTP
)



# ── Erreurs : toujours un statut HTTP et un corps {"error": …} (D4) ──────

@app.exception_handler(CorpsInvalide)
async def _corps_invalide(request: Request, exc: CorpsInvalide):
    """Corps refusé par `app.http.lire_json` (400 illisible, 413 trop gros)."""
    return exc.reponse


@app.exception_handler(RequestValidationError)
async def _validation(request: Request, exc: RequestValidationError):
    """Paramètres de route, de requête ou de formulaire invalides : 400."""
    return JSONResponse(
        {"error": "Paramètres invalides", "detail": jsonable_encoder(exc.errors())},
        status_code=400,
    )


@app.exception_handler(StarletteHTTPException)
async def _http(request: Request, exc: StarletteHTTPException):
    """404 de route inconnue, 405 de méthode refusée… : même forme que les
    autres erreurs (`detail` gardé pour les anciens lecteurs)."""
    message = exc.detail if isinstance(exc.detail, str) else "Erreur"
    return JSONResponse({"error": message, "detail": exc.detail},
                        status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(Exception)
async def _interne(request: Request, exc: Exception):
    """Exception non prévue : message générique, détail dans les logs."""
    logger.error("Erreur non gérée sur %s %s", request.method, request.url.path, exc_info=exc)
    return erreur(ERREUR_INTERNE, 500)


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

for _router in (pages.router, planning.router, courses.router, recettes.router,
                catalogue.router, auth.router, admin.router, cuisine.router):
    app.include_router(_router)
