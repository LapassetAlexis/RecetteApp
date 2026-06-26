"""Couche SQLite — historique des plannings & recettes enrichies."""

import aiosqlite
from datetime import date, datetime
from typing import Any

from app.config import settings


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    return dict(row) if row else {}


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
                    data_json TEXT NOT NULL
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
                    dernier_usage TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_planning_recipes_recipe
                    ON planning_recipes(recipe_name);
                CREATE INDEX IF NOT EXISTS idx_planning_history_week
                    ON planning_history(week_start);
            """)
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
                   (week_start, saison, nb_personnes, ingredients_force, data_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (week_start, saison, nb_personnes, ingredients_force, data_json),
            )
            planning_id = cur.lastrowid

            for r in recipes:
                await db.execute(
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
            await db.commit()
            return planning_id

    async def get_recent_recipe_names(self, weeks: int = 4) -> set[str]:
        """Retourne les noms de recettes utilisées dans les N dernières semaines."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                """SELECT DISTINCT recipe_name FROM planning_recipes pr
                   JOIN planning_history ph ON pr.planning_id = ph.id
                   WHERE ph.created_at >= datetime('now', ? || ' weeks', '-7 days')""",
                (f"-{weeks}",),
            )
            return {r["recipe_name"] for r in rows}

    async def get_last_planning(self) -> dict[str, Any] | None:
        """Retourne le dernier planning généré."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT * FROM planning_history ORDER BY id DESC LIMIT 1"
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

    async def list_plannings(
        self, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Liste les plannings récents."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT id, week_start, created_at, saison, nb_personnes "
                "FROM planning_history ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [_row_to_dict(r) for r in rows]

    # ── Recettes enrichies (cache local) ──────────────────────────

    async def save_enriched(
        self,
        notion_id: str,
        recipe_name: str,
        ingredients: str = "",
        cuisson_minutes: int = 0,
        saison: str = "",
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO enriched_recipes
                   (notion_id, recipe_name, ingredients, cuisson_minutes, saison, dernier_usage)
                   VALUES (?, ?, ?, ?, ?, datetime('now'))""",
                (notion_id, recipe_name, ingredients, cuisson_minutes, saison),
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
