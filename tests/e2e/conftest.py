"""Fixtures des tests de bout en bout (Playwright).

- `server` : l'application démarrée en sous-processus (uvicorn, port libre,
  base SQLite temporaire, Notion simulé par `fake_notion.py`), une fois par
  session. Chaque test crée ses propres plannings.
- `server_auth` / `server_isole` : un serveur neuf par test, avec
  authentification (chef / secret) ou sans (base remplacée par le test).
- `page` (pytest-playwright) : contexte 1280 × 900, service worker bloqué
  (sinon il met en cache des pages entre deux tests), locale française.
- `erreurs_console` (automatique) : toute erreur JavaScript ou console de la
  page fait échouer le test.

Lancement : `.venv/bin/python -m pytest -m e2e` (exclus de la suite par défaut).
"""

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parents[2]


def _port_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Application importée AVANT `fake_notion.serve` : avec AUTH_USER / AUTH_PASSWORD
# dans l'environnement, le middleware d'auth est posé à l'import.
_LANCEUR = ("import sys, app.main; from tests.e2e.fake_notion import serve; "
            "serve(sys.argv[1], int(sys.argv[2]))")


def demarrer(tmp_path_factory, nom: str, env: dict | None = None):
    """Démarre l'application (Notion simulé) sur un port libre et une base
    neuve ; générateur qui produit l'URL puis arrête le serveur."""
    port = _port_libre()
    db = tmp_path_factory.mktemp(nom) / f"{nom}.db"
    log = open(db.with_suffix(".log"), "w")  # noqa: SIM115 — fermé à l'arrêt
    env = {**os.environ, "PYTHONUNBUFFERED": "1", **(env or {})}
    cmd = ([sys.executable, "-c", _LANCEUR] if "AUTH_USER" in env
           else [sys.executable, "-m", "tests.e2e.fake_notion"])
    proc = subprocess.Popen([*cmd, str(db), str(port)], cwd=RACINE, stdout=log,
                            stderr=subprocess.STDOUT, env={**env, "DATABASE_PATH": str(db)})
    url = f"http://127.0.0.1:{port}"
    fin = time.monotonic() + 30
    while True:
        try:
            with urllib.request.urlopen(url + "/health", timeout=1) as r:
                if r.status == 200:
                    break
        except OSError:
            pass
        if proc.poll() is not None or time.monotonic() > fin:
            proc.kill()
            log.close()
            raise RuntimeError(f"Serveur e2e ({nom}) non démarré :\n" + db.with_suffix(".log").read_text())
        time.sleep(0.1)
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    yield from demarrer(tmp_path_factory, "e2e")


@pytest.fixture
def server_auth(tmp_path_factory):
    """Serveur avec authentification (chef / secret), un par test : la limite
    des échecs de connexion (en mémoire) ne déborde pas d'un test à l'autre."""
    yield from demarrer(tmp_path_factory, "e2e-auth", {"AUTH_USER": "chef", "AUTH_PASSWORD": "secret"})


@pytest.fixture
def server_isole(tmp_path_factory):
    """Serveur sans auth, un par test, pour les parcours qui remplacent toute
    la base (import « replace » de /admin)."""
    yield from demarrer(tmp_path_factory, "e2e-isole")


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {
        **browser_context_args,
        "viewport": {"width": 1280, "height": 900},
        "locale": "fr-FR",
        "service_workers": "block",
    }


@pytest.fixture(autouse=True)
def erreurs_console(request):
    """Échoue si la page (fixture `page`) a levé une erreur JS ou écrit une
    erreur en console. Les tests sans `page` ne sont pas concernés."""
    if "page" not in request.fixturenames:
        yield []
        return
    page = request.getfixturevalue("page")
    erreurs: list[str] = []
    page.on("pageerror", lambda e: erreurs.append(f"pageerror: {e}"))
    page.on("console", lambda m: erreurs.append(f"console: {m.text}") if m.type == "error" else None)
    yield erreurs
    assert not erreurs, "Erreurs dans la page :\n" + "\n".join(erreurs)
