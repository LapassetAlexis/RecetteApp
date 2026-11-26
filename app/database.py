"""Couche SQLite — historique des plannings & recettes enrichies."""

import json
from datetime import date, timedelta
from typing import Any

import aiosqlite

from app.config import settings


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    return dict(row) if row else {}


def _share_token_of(data_json: str | None) -> str | None:
    """Jeton de partage public porté par un data_json, None s'il n'y en a pas.

    Lecture tolérante : un JSON illisible ou sans `share_token` donne None."""
    try:
        data = json.loads(data_json or "")
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    token = data.get("share_token")
    return str(token) if token else None


# Colonnes exportées/importées (la colonne `share_token` n'y figure pas : elle
# est dérivée du data_json et recalculée à l'import).
PLANNING_COLUMNS = ("id", "week_start", "created_at", "saison", "nb_personnes",
                    "ingredients_force", "data_json", "valide")
PLANNING_RECIPE_COLUMNS = ("notion_id", "recipe_name", "repas_type", "jour", "moment")
ENRICHED_COLUMNS = ("notion_id", "recipe_name", "ingredients", "cuisson_minutes", "saison",
                    "dernier_usage", "nutrition", "base_servings")


async def _insert_planning_recipes(
    conn: aiosqlite.Connection, planning_id: int, recipes: list[dict[str, Any]],
) -> None:
    """Insère les lignes `planning_recipes` d'un planning (sans commit)."""
    for r in recipes:
        await conn.execute(
            """INSERT INTO planning_recipes
               (planning_id, notion_id, recipe_name, repas_type, jour, moment)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                planning_id,
                r.get("notion_id"),
                r["recipe_name"],
                r.get("repas_type", ""),
                r["jour"],
                r["moment"],
            ),
        )


class Database:
    """Gère les tables locales : historique des plannings, cache recettes."""

    def __init__(self) -> None:
        self.path = settings.database_path

    async def init(self) -> None:
        """Crée les tables si elles n'existent pas."""
        async with aiosqlite.connect(self.path) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS planning_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    week_start TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    saison TEXT,
                    nb_personnes INTEGER DEFAULT 4,
                    ingredients_force TEXT,
                    data_json TEXT NOT NULL,
                    valide INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS planning_recipes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    planning_id INTEGER NOT NULL,
                    notion_id TEXT,
                    recipe_name TEXT NOT NULL,
                    repas_type TEXT,
                    jour INTEGER NOT NULL,
                    moment TEXT NOT NULL,
                    FOREIGN KEY (planning_id) REFERENCES planning_history(id)
                );

                CREATE TABLE IF NOT EXISTS enriched_recipes (
                    notion_id TEXT PRIMARY KEY,
                    recipe_name TEXT NOT NULL,
                    ingredients TEXT,
                    cuisson_minutes INTEGER,
                    saison TEXT,
                    dernier_usage TEXT,
                    nutrition TEXT,
                    base_servings INTEGER DEFAULT 4
                );

                CREATE INDEX IF NOT EXISTS idx_planning_recipes_recipe
                    ON planning_recipes(recipe_name);
                CREATE INDEX IF NOT EXISTS idx_planning_history_week
                    ON planning_history(week_start);

                CREATE TABLE IF NOT EXISTS app_prefs (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    data_json TEXT NOT NULL
                );
            """)
            # Migrations idempotentes : on ajoute les colonnes manquantes en
            # vérifiant d'abord leur présence (PRAGMA). Pas de try/except
            # silencieux : une vraie erreur (droits, base verrouillée) doit
            # remonter plutôt que laisser un 500 plus tard.
            async def _has_column(table: str, col: str) -> bool:
                rows = await db.execute_fetchall(f"PRAGMA table_info({table})")
                return any(r[1] == col for r in rows)

            if not await _has_column("planning_history", "valide"):
                await db.execute(
                    "ALTER TABLE planning_history ADD COLUMN valide INTEGER NOT NULL DEFAULT 0"
                )
                # Les plannings existants étaient validés sous l'ancien comportement
                await db.execute("UPDATE planning_history SET valide = 1")
            if not await _has_column("enriched_recipes", "nutrition"):
                await db.execute("ALTER TABLE enriched_recipes ADD COLUMN nutrition TEXT")
            if not await _has_column("enriched_recipes", "base_servings"):
                # Base de portions par recette (défaut 4 pour les lignes existantes,
                # ce qui reproduit l'ancien comportement global BASE_SERVINGS=4).
                await db.execute(
                    "ALTER TABLE enriched_recipes ADD COLUMN base_servings INTEGER DEFAULT 4"
                )
            if not await _has_column("planning_history", "share_token"):
                # Jeton de partage public en colonne indexée (il n'était stocké
                # que dans data_json, retrouvé en parcourant tous les plannings).
                await db.execute("ALTER TABLE planning_history ADD COLUMN share_token TEXT")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_planning_history_share_token "
                "ON planning_history(share_token)"
            )
            # Remplissage depuis data_json des lignes qui n'ont pas encore la
            # colonne à jour (premier démarrage après migration). Idempotent.
            rows = await db.execute_fetchall(
                "SELECT id, data_json FROM planning_history "
                "WHERE share_token IS NULL AND data_json LIKE '%share_token%'"
            )
            for pid, data_json in rows:
                token = _share_token_of(data_json)
                if token:
                    await db.execute(
                        "UPDATE planning_history SET share_token = ? WHERE id = ?", (token, pid)
                    )
            await db.commit()

    # ── Historique des plannings ──────────────────────────────────

    async def save_planning(
        self,
        week_start: str,
        saison: str,
        nb_personnes: int,
        ingredients_force: str,
        data_json: str,
        recipes: list[dict[str, Any]],
    ) -> int:
        """Sauvegarde un planning et retourne son ID."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """INSERT INTO planning_history
                   (week_start, saison, nb_personnes, ingredients_force, data_json, share_token)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (week_start, saison, nb_personnes, ingredients_force, data_json,
                 _share_token_of(data_json)),
            )
            planning_id = cur.lastrowid
            await _insert_planning_recipes(db, planning_id, recipes)
            await db.commit()
            return planning_id

    async def update_planning(
        self,
        planning_id: int,
        data_json: str,
        recipes: list[dict[str, Any]],
        week_start: str | None = None,
        nb_personnes: int | None = None,
    ) -> None:
        """Met à jour un planning existant (data_json + recettes) sans en créer un nouveau.

        `week_start` et `nb_personnes` ne sont modifiés que s'ils sont fournis
        (re-soumission d'un brouillon avec une autre date de début)."""
        sets, params = ["data_json = ?", "share_token = ?"], [data_json, _share_token_of(data_json)]
        if week_start:
            sets.append("week_start = ?")
            params.append(week_start)
        if nb_personnes is not None:
            sets.append("nb_personnes = ?")
            params.append(nb_personnes)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                f"UPDATE planning_history SET {', '.join(sets)} WHERE id = ?",
                (*params, planning_id),
            )
            await db.execute(
                "DELETE FROM planning_recipes WHERE planning_id = ?",
                (planning_id,),
            )
            await _insert_planning_recipes(db, planning_id, recipes)
            await db.commit()

    async def update_planning_data(self, planning_id: int, data_json: str) -> None:
        """Met à jour uniquement le data_json d'un planning (ex. liste de courses).

        La colonne `share_token` suit le jeton éventuellement présent dans le
        data_json (le partage l'y écrit)."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE planning_history SET data_json = ?, share_token = ? WHERE id = ?",
                (data_json, _share_token_of(data_json), planning_id),
            )
            await db.commit()

    async def mark_planning_valid(self, planning_id: int) -> None:
        """Valide un planning (brouillon -> enregistré dans l'historique)."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE planning_history SET valide = 1 WHERE id = ?", (planning_id,)
            )
            await db.commit()

    async def list_drafts(self, limit: int = 20) -> list[dict[str, Any]]:
        """Liste les brouillons (plannings non validés), du plus récent au plus ancien."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT id, week_start, created_at, saison, nb_personnes, data_json "
                "FROM planning_history WHERE valide = 0 ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [_row_to_dict(r) for r in rows]

    async def delete_planning(self, planning_id: int) -> None:
        """Supprime un planning (validé ou brouillon) et ses recettes liées."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM planning_recipes WHERE planning_id = ?", (planning_id,)
            )
            await db.execute(
                "DELETE FROM planning_history WHERE id = ?", (planning_id,)
            )
            await db.commit()

    async def get_last_planning(self) -> dict[str, Any] | None:
        """Retourne le dernier planning VALIDÉ."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT * FROM planning_history WHERE valide = 1 ORDER BY id DESC LIMIT 1"
            )
            if not rows:
                return None
            return _row_to_dict(rows[0])

    async def get_planning_with_recipes(
        self, planning_id: int
    ) -> dict[str, Any] | None:
        """Retourne un planning avec ses recettes."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT * FROM planning_history WHERE id = ?", (planning_id,)
            )
            if not rows:
                return None
            planning = _row_to_dict(rows[0])

            recipe_rows = await db.execute_fetchall(
                "SELECT * FROM planning_recipes WHERE planning_id = ? ORDER BY jour, moment",
                (planning_id,),
            )
            planning["recipes"] = [_row_to_dict(r) for r in recipe_rows]
            return planning

    async def get_planning_by_share_token(self, token: str) -> dict[str, Any] | None:
        """Retrouve le planning partagé sous ce jeton (colonne indexée
        `share_token`, tenue à jour à chaque écriture du data_json)."""
        if not token:
            return None
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT * FROM planning_history WHERE share_token = ? ORDER BY id DESC LIMIT 1",
                (token,),
            )
        return _row_to_dict(rows[0]) if rows else None

    async def list_plannings(
        self, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Liste les plannings récents."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT id, week_start, created_at, saison, nb_personnes, data_json "
                "FROM planning_history WHERE valide = 1 ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [_row_to_dict(r) for r in rows]

    async def get_recipe_usage(self) -> dict[str, dict[str, Any]]:
        """Usage des recettes dans les plannings VALIDÉS.

        Retourne {notion_id: {"count": n, "last": "AAAA-MM-JJ"}} : `count` =
        nombre de repas où la recette apparaît, `last` = date du repas le plus
        récent (week_start = date du jour 1, donc date = week_start + jour − 1).
        Les repas sans notion_id (repas libres) sont ignorés."""
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall(
                """SELECT pr.notion_id, pr.jour, ph.week_start
                   FROM planning_recipes pr
                   JOIN planning_history ph ON pr.planning_id = ph.id
                   WHERE ph.valide = 1 AND pr.notion_id IS NOT NULL AND pr.notion_id != ''"""
            )
        usage: dict[str, dict[str, Any]] = {}
        for nid, jour, week_start in rows:
            entry = usage.setdefault(nid, {"count": 0, "last": ""})
            entry["count"] += 1
            try:
                d = (date.fromisoformat(week_start) + timedelta(days=int(jour) - 1)).isoformat()
            except (TypeError, ValueError):
                continue
            if d > entry["last"]:
                entry["last"] = d
        return usage

    # ── Recettes enrichies (cache local) ──────────────────────────

    async def save_enriched(
        self,
        notion_id: str,
        recipe_name: str,
        ingredients: str | None = None,
        cuisson_minutes: int | None = None,
        saison: str | None = None,
        nutrition: str | None = None,
        base_servings: int | None = None,
    ) -> None:
        """Crée ou met à jour l'entrée de cache d'une recette.

        Upsert partiel : un champ laissé à `None` conserve la valeur déjà en
        base (une ré-extraction d'ingrédients n'efface plus la durée, la
        nutrition ni la base de portions). À la création, `base_servings`
        retombe sur 4 (ancien BASE_SERVINGS global)."""
        params = {
            "nid": notion_id, "name": recipe_name, "ings": ingredients,
            "cuisson": cuisson_minutes, "saison": saison,
            "nutri": nutrition, "bs": base_servings,
        }
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT INTO enriched_recipes
                   (notion_id, recipe_name, ingredients, cuisson_minutes, saison,
                    dernier_usage, nutrition, base_servings)
                   VALUES (:nid, :name, COALESCE(:ings, ''), COALESCE(:cuisson, 0),
                           COALESCE(:saison, ''), datetime('now'),
                           COALESCE(:nutri, ''), COALESCE(:bs, 4))
                   ON CONFLICT(notion_id) DO UPDATE SET
                     recipe_name = excluded.recipe_name,
                     dernier_usage = excluded.dernier_usage,
                     ingredients = COALESCE(:ings, enriched_recipes.ingredients),
                     cuisson_minutes = COALESCE(:cuisson, enriched_recipes.cuisson_minutes),
                     saison = COALESCE(:saison, enriched_recipes.saison),
                     nutrition = COALESCE(:nutri, enriched_recipes.nutrition),
                     base_servings = COALESCE(:bs, enriched_recipes.base_servings)""",
                params,
            )
            await db.commit()

    async def get_enriched(self, notion_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT * FROM enriched_recipes WHERE notion_id = ?",
                (notion_id,),
            )
            return _row_to_dict(rows[0]) if rows else None

    async def delete_enriched(self, notion_id: str) -> None:
        """Purge le cache local d'une recette supprimée."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM enriched_recipes WHERE notion_id = ?", (notion_id,)
            )
            await db.commit()

    async def get_all_enriched_ingredients(self) -> dict[str, str]:
        """{notion_id: noms d'ingrédients en minuscules} pour la recherche par
        ingrédient (1 seule requête au lieu de N)."""
        out: dict[str, str] = {}
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall(
                "SELECT notion_id, ingredients FROM enriched_recipes WHERE ingredients IS NOT NULL"
            )
        for nid, ings in rows:
            try:
                noms = [str(i.get("nom", "")) for i in json.loads(ings)]
                out[nid] = " ".join(noms).lower()
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
        return out

    async def get_all_enriched_durations(self) -> dict[str, int]:
        """{notion_id: durée en minutes} pour les recettes ayant un temps connu."""
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall(
                "SELECT notion_id, cuisson_minutes FROM enriched_recipes "
                "WHERE cuisson_minutes IS NOT NULL AND cuisson_minutes > 0"
            )
        return {nid: int(m) for nid, m in rows}

    # ── Réglages du foyer (table app_prefs, une seule ligne) ──────

    async def get_app_prefs(self) -> dict[str, Any]:
        """Réglages uniques du foyer (placard, ordre des rayons…), {} si aucun.

        Lecture tolérante : un JSON illisible ou qui n'est pas un objet donne {}."""
        async with aiosqlite.connect(self.path) as db:
            return await _read_prefs(db)

    async def save_app_prefs(self, data: dict[str, Any]) -> None:
        """Remplace les réglages du foyer (ligne unique id = 1)."""
        async with aiosqlite.connect(self.path) as db:
            await _write_prefs(db, data)
            await db.commit()

    # ── Sauvegarde / restauration (cf. app/services/backup.py) ────

    async def dump_all(self) -> dict[str, Any]:
        """Lignes brutes de toutes les tables de données, pour l'export :
        {"plannings": [... avec "recipes"], "enriched": [...], "prefs": {...}}."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            plannings = [
                _row_to_dict(r) for r in await db.execute_fetchall(
                    f"SELECT {', '.join(PLANNING_COLUMNS)} FROM planning_history ORDER BY id"
                )
            ]
            recipes: dict[int, list[dict[str, Any]]] = {}
            for r in await db.execute_fetchall(
                f"SELECT planning_id, {', '.join(PLANNING_RECIPE_COLUMNS)} "
                "FROM planning_recipes ORDER BY planning_id, id"
            ):
                row = _row_to_dict(r)
                recipes.setdefault(row.pop("planning_id"), []).append(row)
            for p in plannings:
                p["recipes"] = recipes.get(p["id"], [])
            enriched = [
                _row_to_dict(r) for r in await db.execute_fetchall(
                    f"SELECT {', '.join(ENRICHED_COLUMNS)} FROM enriched_recipes ORDER BY notion_id"
                )
            ]
            prefs = await _read_prefs(db)
        return {"plannings": plannings, "enriched": enriched, "prefs": prefs}

    async def counts(self) -> dict[str, int]:
        """Nombre de plannings validés, de brouillons et de recettes en cache."""
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall(
                "SELECT COALESCE(SUM(valide = 1), 0), COALESCE(SUM(valide = 0), 0) FROM planning_history"
            )
            valides, brouillons = rows[0]
            rows = await db.execute_fetchall("SELECT COUNT(*) FROM enriched_recipes")
        return {"plannings": int(valides), "brouillons": int(brouillons), "enriched": int(rows[0][0])}

    async def restore_all(
        self,
        plannings: list[dict[str, Any]],
        enriched: list[dict[str, Any]],
        prefs: dict[str, Any],
        replace: bool,
    ) -> dict[str, int]:
        """Restaure des données exportées, en UNE transaction (tout ou rien).

        - `replace=True` : vide les tables puis réinsère tout, ids d'origine
          conservés (les liens /planning/<id> restent valides).
        - `replace=False` (fusion) : ajoute les plannings absents (clé
          week_start + created_at) sous un nouvel id, les recettes en cache
          absentes (une entrée locale n'est jamais écrasée) et les réglages
          dont la clé manque localement.

        Les données doivent avoir été validées en amont. Retourne le nombre de
        plannings et de recettes effectivement insérés."""
        added = {"plannings": 0, "enriched": 0}
        async with aiosqlite.connect(self.path) as db:
            try:
                if replace:
                    for table in ("planning_recipes", "planning_history", "enriched_recipes", "app_prefs"):
                        await db.execute(f"DELETE FROM {table}")
                    existing: set[tuple[str, str]] = set()
                    current_prefs: dict[str, Any] = {}
                else:
                    existing = {
                        (w, c) for w, c in await db.execute_fetchall(
                            "SELECT week_start, created_at FROM planning_history"
                        )
                    }
                    current_prefs = await _read_prefs(db)

                cols = [c for c in PLANNING_COLUMNS if replace or c != "id"]
                for p in plannings:
                    if (p["week_start"], p["created_at"]) in existing:
                        continue
                    cur = await db.execute(
                        f"INSERT INTO planning_history ({', '.join(cols)}, share_token) "
                        f"VALUES ({', '.join('?' * len(cols))}, ?)",
                        (*(p.get(c) for c in cols), _share_token_of(p.get("data_json"))),
                    )
                    await _insert_planning_recipes(db, cur.lastrowid, p.get("recipes", []))
                    added["plannings"] += 1
                    if not replace:
                        existing.add((p["week_start"], p["created_at"]))

                verb = "INSERT" if replace else "INSERT OR IGNORE"
                for e in enriched:
                    cur = await db.execute(
                        f"{verb} INTO enriched_recipes ({', '.join(ENRICHED_COLUMNS)}) "
                        f"VALUES ({', '.join('?' * len(ENRICHED_COLUMNS))})",
                        tuple(e.get(c) for c in ENRICHED_COLUMNS),
                    )
                    added["enriched"] += cur.rowcount

                # Fusion : les réglages locaux l'emportent clé par clé.
                merged_prefs = {**prefs, **current_prefs}
                if merged_prefs:
                    await _write_prefs(db, merged_prefs)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return added


async def _read_prefs(db: aiosqlite.Connection) -> dict[str, Any]:
    """Réglages du foyer lus sur une connexion ouverte ({} si absents ou illisibles)."""
    rows = await db.execute_fetchall("SELECT data_json FROM app_prefs WHERE id = 1")
    if not rows:
        return {}
    try:
        data = json.loads(rows[0][0])
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


async def _write_prefs(db: aiosqlite.Connection, data: dict[str, Any]) -> None:
    """Écrit la ligne unique de réglages (sans commit)."""
    await db.execute(
        """INSERT INTO app_prefs (id, data_json) VALUES (1, ?)
           ON CONFLICT(id) DO UPDATE SET data_json = excluded.data_json""",
        (json.dumps(data, ensure_ascii=False),),
    )
