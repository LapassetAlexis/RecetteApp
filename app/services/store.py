"""Écritures d'un planning sous verrou.

Toute écriture du `data_json` d'un planning passe par `update_planning` : la
relecture, la mutation et l'écriture se font sous un verrou propre au
planning. Deux requêtes simultanées (un changement de repas et une coche du
lien public, par exemple) s'exécutent donc l'une après l'autre, chacune sur
l'état laissé par la précédente : aucune ne réécrit un `data_json` périmé.

Les appels lents (Notion) se font AVANT, hors verrou : la fonction passée à
`update_planning` ne fait que relire, modifier et calculer.

Les verrous vivent en mémoire : valable avec un seul processus uvicorn
(`entrypoint.sh`), comme le reste de l'état en mémoire de l'application.
"""

import asyncio
import inspect
import json
from contextlib import asynccontextmanager

from app.deps import db
from app.services.planning import recipe_rows

# Clé : (boucle asyncio, identifiant). Un asyncio.Lock est lié à la boucle où
# il a servi la première fois ; les tests ouvrent une boucle par appel.
_locks: dict[tuple[int, object], asyncio.Lock] = {}


def _lock(key) -> asyncio.Lock:
    return _locks.setdefault((id(asyncio.get_running_loop()), key), asyncio.Lock())


@asynccontextmanager
async def planning_lock(planning_id: int):
    """Verrou d'écriture d'un planning."""
    async with _lock(("planning", int(planning_id))):
        yield


@asynccontextmanager
async def prefs_lock():
    """Verrou d'écriture des réglages du foyer (`app_prefs`)."""
    async with _lock("prefs"):
        yield


class NoWrite:
    """Résultat d'une mutation qui ne doit rien écrire (erreur, rien à faire).

    `update_planning` renvoie alors `value` sans toucher à la base."""

    __slots__ = ("value",)

    def __init__(self, value=None):
        self.value = value


def load(raw) -> dict:
    """`data_json` lu de façon tolérante : {} s'il est vide, illisible ou pas
    un objet."""
    try:
        data = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        data = {}
    return data if isinstance(data, dict) else {}


_load = load   # ancien nom (routers/courses.py) ; retiré par M11


async def update_planning(planning_id: int, fn, recipes: bool = False, **columns):
    """Lit le planning, applique `fn(planning, data)` puis écrit, sous verrou.

    - `fn` est synchrone ou asynchrone ; elle modifie `data` en place. Si elle
      renvoie `NoWrite(valeur)`, rien n'est écrit et `valeur` est renvoyée ;
    - `recipes=True` réécrit aussi `planning_recipes` depuis `data["plats"]`
      dans la même transaction (les deux restent cohérents) ;
    - `columns` (`week_start`, `nb_personnes`) sont écrites avec, si fournies.

    Renvoie `(data, résultat de fn)`, ou `(None, None)` si le planning n'existe
    pas."""
    async with planning_lock(planning_id):
        planning = await db.get_planning_with_recipes(planning_id)
        if not planning:
            return None, None
        data = load(planning.get("data_json"))
        res = fn(planning, data)
        if inspect.isawaitable(res):
            res = await res
        if isinstance(res, NoWrite):
            return data, res.value
        data_json = json.dumps(data, ensure_ascii=False)
        if recipes or columns:
            await db.update_planning(
                planning_id=planning_id, data_json=data_json,
                recipes=recipe_rows(data.get("plats") or []), **columns,
            )
        else:
            await db.update_planning_data(planning_id, data_json)
        return data, res
