"""Client Notion API — lecture / écriture de la base de recettes."""

import httpx
from typing import Any

from app.config import settings

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
        """Transforme une page Notion en dict structuré."""
        p = page["properties"]
        # Nom (title)
        nom = ""
        if p["Nom"]["type"] == "title" and p["Nom"]["title"]:
            nom = p["Nom"]["title"][0]["plain_text"]

        # URL
        url = ""
        if p["URL"]["type"] == "url" and p["URL"]["url"]:
            url = p["URL"]["url"]

        # Repas (select)
        repas = ""
        if p["Repas"]["select"]:
            repas = p["Repas"]["select"]["name"]

        # Tag (multi_select)
        tags = []
        if p["Tag"]["multi_select"]:
            tags = [t["name"] for t in p["Tag"]["multi_select"]]

        # Note (select)
        note = ""
        if p["Note"]["select"]:
            note = p["Note"]["select"]["name"]

        # État (status)
        etat = ""
        if p["État"]["status"]:
            etat = p["État"]["status"]["name"]

        return {
            "id": page["id"],
            "nom": nom,
            "url": url,
            "repas": repas,
            "tags": tags,
            "note": note,
            "etat": etat,
        }

    # ── Création d'une fiche ──────────────────────────────────────

    async def create_recipe(
        self,
        nom: str,
        url: str = "",
        repas: str = "",
        tags: list[str] | None = None,
        etat: str = "À essayer",
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

    # ── Mise à jour ───────────────────────────────────────────────

    async def update_recipe(
        self,
        page_id: str,
        nom: str | None = None,
        url: str | None = None,
        repas: str | None = None,
        tags: list[str] | None = None,
        note: str | None = None,
        etat: str | None = None,
    ) -> dict[str, Any]:
        """Met à jour une page existante."""
        properties: dict[str, Any] = {}

        if nom is not None:
            properties["Nom"] = {"title": [{"text": {"content": nom}}]}
        if url is not None:
            properties["URL"] = {"url": url}
        if repas is not None:
            properties["Repas"] = {"select": {"name": repas}}
        if tags is not None:
            properties["Tag"] = {"multi_select": [{"name": t} for t in tags]}
        if note is not None:
            properties["Note"] = {"select": {"name": note}}
        if etat is not None:
            properties["État"] = {"status": {"name": etat}}

        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{BASE_URL}/pages/{page_id}",
                headers=self._headers,
                json={"properties": properties},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
