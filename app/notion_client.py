"""Client Notion API — lecture / écriture de la base de recettes."""

import httpx
import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)
NOTION_VERSION = "2022-06-28"
BASE_URL = "https://api.notion.com/v1"


class NotionClient:
    """Wrapper autour de l'API Notion pour la base Livre de recettes."""

    def __init__(self) -> None:
        self.token = settings.notion_token
        self.database_id = settings.notion_database_id
        self._headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    # ── Récupération des recettes ──────────────────────────────────

    async def get_all_recipes(self) -> list[dict[str, Any]]:
        """Parcourt toutes les pages de la base et retourne les recettes."""
        recipes: list[dict[str, Any]] = []
        start_cursor: str | None = None

        async with httpx.AsyncClient() as client:
            while True:
                body: dict[str, Any] = {"page_size": 100}
                if start_cursor:
                    body["start_cursor"] = start_cursor

                resp = await client.post(
                    f"{BASE_URL}/databases/{self.database_id}/query",
                    headers=self._headers,
                    json=body,
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()

                for page in data.get("results", []):
                    recipe = self._parse_page(page)
                    if recipe["nom"]:  # ignorer les pages sans titre
                        recipes.append(recipe)

                if not data.get("has_more"):
                    break
                start_cursor = data.get("next_cursor")

        return recipes

    def _parse_page(self, page: dict[str, Any]) -> dict[str, Any]:
        """Transforme une page Notion en dict structuré.

        Tolérant aux colonnes manquantes/renommées : une propriété absente
        donne une valeur vide au lieu de faire planter toute la récupération.
        """
        p = page.get("properties", {})

        # Nom (title)
        nom = ""
        title = p.get("Nom", {}).get("title") or []
        if title:
            nom = title[0].get("plain_text", "")

        # URL
        url = p.get("URL", {}).get("url") or ""

        # Repas (select)
        repas = ""
        sel = p.get("Repas", {}).get("select")
        if sel:
            repas = sel.get("name", "")

        # Tag (multi_select)
        tags = [t.get("name", "") for t in (p.get("Tag", {}).get("multi_select") or [])]

        # Note (select)
        note = ""
        sel = p.get("Note", {}).get("select")
        if sel:
            note = sel.get("name", "")

        # État (status)
        etat = ""
        st = p.get("État", {}).get("status")
        if st:
            etat = st.get("name", "")

        # Moment (select) - peut ne pas exister
        moment = ""
        sel = p.get("Moment", {}).get("select")
        if sel:
            moment = sel.get("name", "")

        return {
            "id": page["id"],
            "nom": nom,
            "url": url,
            "notion_url": page.get("url", ""),
            "repas": repas,
            "tags": tags,
            "note": note,
            "etat": etat,
            "moment": moment,
        }

    # ── Création d'une fiche ──────────────────────────────────────

    async def create_recipe(
        self,
        nom: str,
        url: str = "",
        repas: str = "",
        tags: list[str] | None = None,
        etat: str = "À essayer",
        moment: str = "",
    ) -> dict[str, Any]:
        """Crée une nouvelle page dans la base de recettes."""
        properties: dict[str, Any] = {
            "Nom": {"title": [{"text": {"content": nom}}]},
            "URL": {"url": url},
            "État": {"status": {"name": etat}},
        }

        if repas:
            properties["Repas"] = {"select": {"name": repas}}
        if tags:
            properties["Tag"] = {
                "multi_select": [{"name": t} for t in tags]
            }
        if moment:
            properties["Moment"] = {"select": {"name": moment}}

        body = {
            "parent": {"database_id": self.database_id},
            "properties": properties,
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{BASE_URL}/pages",
                headers=self._headers,
                json=body,
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()

    # ── Mise à jour avec ingrédients ─────────────────────────────

    async def update_ingredients(
        self,
        page_id: str,
        ingredients_text: str,
    ) -> dict[str, Any]:
        """Ajoute les ingrédients dans la propriété Ingrédients (colonne Notion)."""
        # Essayer d'abord la propriété (colonne du tableau)
        properties = {
            "Ingrédients": {
                "rich_text": [{"text": {"content": ingredients_text[:2000]}}]
            }
        }
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{BASE_URL}/pages/{page_id}",
                headers=self._headers,
                json={"properties": properties, "icon": {"emoji": "🥗"}},
                timeout=30,
            )
            if resp.status_code == 400:
                # La propriété n'existe pas → écrire en blocks dans le corps
                return await self._append_ingredients_blocks(page_id, ingredients_text)
            resp.raise_for_status()
            return resp.json()

    async def _append_ingredients_blocks(
        self, page_id: str, ingredients_text: str
    ) -> dict[str, Any]:
        """Fallback : écrit les ingrédients en blocks dans le corps de la page."""
        lines = [l.strip() for l in ingredients_text.split("\n") if l.strip()]
        children = []
        for line in lines:
            text = line.lstrip("- ").strip()
            if text:
                children.append({
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [{"type": "text", "text": {"content": text[:200]}}]
                    }
                })
        if not children:
            return {}

        blocks = [
            {"object": "block", "type": "heading_3", "heading_3": {
                "rich_text": [{"type": "text", "text": {"content": "📝 Ingrédients"}}]
            }}
        ] + children

        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{BASE_URL}/blocks/{page_id}/children",
                headers=self._headers,
                json={"children": blocks},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()

    async def ensure_ingredients_field(self) -> bool:
        """Vérifie/crée le champ Ingrédients + Moment dans la base Notion."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{BASE_URL}/databases/{self.database_id}",
                headers=self._headers,
                timeout=30,
            )
            resp.raise_for_status()
            db = resp.json()
            props = db.get("properties", {})
            update_props = {}

            if "Ingrédients" not in props:
                update_props["Ingrédients"] = {"rich_text": {}}

            if "Moment" not in props:
                update_props["Moment"] = {
                    "select": {
                        "options": [
                            {"name": "Midi", "color": "orange"},
                            {"name": "Soir", "color": "purple"},
                            {"name": "Les deux", "color": "green"},
                        ]
                    }
                }

            if update_props:
                await client.patch(
                    f"{BASE_URL}/databases/{self.database_id}",
                    headers=self._headers,
                    json={"properties": update_props},
                    timeout=30,
                )
                logger.info(f"✅ Champs créés dans Notion: {list(update_props.keys())}")
            return True

    # ── Mise à jour de l'image ───────────────────────────────────

    async def update_image(
        self,
        page_id: str,
        image_url: str,
    ) -> dict[str, Any]:
        """Définit l'image de couverture d'une page Notion."""
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{BASE_URL}/pages/{page_id}",
                headers=self._headers,
                json={
                    "cover": {
                        "type": "external",
                        "external": {"url": image_url}
                    }
                },
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()

    # ── Ajout d'instructions dans le corps ──────────────────────

    async def append_instructions(
        self,
        page_id: str,
        instructions_text: str,
    ) -> dict[str, Any]:
        """Ajoute les instructions comme blocks dans le corps de la page."""
        lines = [l.strip() for l in instructions_text.split("\n") if l.strip()]
        if not lines:
            return {}
        children = []
        for line in lines:
            text = line.lstrip("0123456789. -–*").strip()
            if text:
                children.append({
                    "object": "block",
                    "type": "numbered_list_item",
                    "numbered_list_item": {
                        "rich_text": [{"type": "text", "text": {"content": text[:500]}}]
                    }
                })
        if not children:
            return {}
        blocks = [
            {"object": "block", "type": "heading_3", "heading_3": {
                "rich_text": [{"type": "text", "text": {"content": "👨‍🍳 Instructions"}}]
            }}
        ] + children

        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{BASE_URL}/blocks/{page_id}/children",
                headers=self._headers,
                json={"children": blocks},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()


    # ── Mise à jour de la note ──────────────────────────────────

    async def update_rating(
        self,
        page_id: str,
        note: str,
    ) -> dict[str, Any]:
        """Met à jour la note d'une recette (⭐ à ⭐⭐⭐⭐⭐)."""
        properties = {
            "Note": {
                "select": {"name": note}
            }
        }
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{BASE_URL}/pages/{page_id}",
                headers=self._headers,
                json={"properties": properties},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
