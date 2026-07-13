"""Couche SQLite — historique des plannings & recettes enrichies."""

import aiosqlite
import json
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
                    nutrition TEXT
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

    async def update_planning(
        self,
        planning_id: int,
        data_json: str,
        recipes: list[dict[str, Any]],
    ) -> None:
        """Met à jour un planning existant (data_json + recettes) sans en créer un nouveau."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE planning_history SET data_json = ? WHERE id = ?",
                (data_json, planning_id),
            )
            await db.execute(
                "DELETE FROM planning_recipes WHERE planning_id = ?",
                (planning_id,),
            )
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

    async def update_planning_data(self, planning_id: int, data_json: str) -> None:
        """Met à jour uniquement le data_json d'un planning (ex. liste de courses)."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE planning_history SET data_json = ? WHERE id = ?",
                (data_json, planning_id),
            )
            await db.commit()

    async def get_recent_recipe_names(self, weeks: int = 4) -> set[str]:
        """Retourne les noms de recettes utilisées dans les N dernières semaines."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            # SQLite ne connaît PAS le modificateur 'weeks' (renvoie NULL et
            # n'exclut rien). On convertit en jours : N semaines + 7 j de marge.
            rows = await db.execute_fetchall(
                """SELECT DISTINCT recipe_name FROM planning_recipes pr
                   JOIN planning_history ph ON pr.planning_id = ph.id
                   WHERE ph.valide = 1 AND ph.created_at >= datetime('now', ? || ' days')""",
                (f"-{weeks * 7 + 7}",),
            )
            return {r["recipe_name"] for r in rows}

    async def mark_planning_valid(self, planning_id: int) -> None:
        """Valide un planning (brouillon -> enregistré dans l'historique)."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE planning_history SET valide = 1 WHERE id = ?", (planning_id,)
            )
            await db.commit()

    async def delete_draft_plannings(self) -> None:
        """Supprime les brouillons non validés (et leurs recettes liées)."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """DELETE FROM planning_recipes
                   WHERE planning_id IN (SELECT id FROM planning_history WHERE valide = 0)"""
            )
            await db.execute("DELETE FROM planning_history WHERE valide = 0")
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

    # ── Préférences de génération (mémorisées entre sessions) ─────

    async def save_prefs(self, data_json: str) -> None:
        """Mémorise les derniers paramètres de génération (1 seule ligne)."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO app_prefs (id, data_json) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET data_json = excluded.data_json",
                (data_json,),
            )
            await db.commit()

    async def get_prefs(self) -> dict[str, Any]:
        """Retourne les derniers paramètres mémorisés ({} si aucun)."""
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall("SELECT data_json FROM app_prefs WHERE id = 1")
            if not rows:
                return {}
            try:
                return json.loads(rows[0][0])
            except (json.JSONDecodeError, TypeError):
                return {}

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
        """Retrouve un planning dont le data_json contient ce share_token.

        Pas de colonne dédiée : on filtre en Python sur le JSON (le nombre de
        plannings reste petit, et le partage est une action ponctuelle)."""
        if not token:
            return None
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT * FROM planning_history ORDER BY id DESC"
            )
            for row in rows:
                planning = _row_to_dict(row)
                try:
                    data = json.loads(planning["data_json"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if data.get("share_token") == token:
                    return planning
        return None

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

    # ── Recettes enrichies (cache local) ──────────────────────────

    async def save_enriched(
        self,
        notion_id: str,
        recipe_name: str,
        ingredients: str = "",
        cuisson_minutes: int = 0,
        saison: str = "",
        nutrition: str = "",
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO enriched_recipes
                   (notion_id, recipe_name, ingredients, cuisson_minutes, saison, dernier_usage, nutrition)
                   VALUES (?, ?, ?, ?, ?, datetime('now'), ?)""",
                (notion_id, recipe_name, ingredients, cuisson_minutes, saison, nutrition),
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
