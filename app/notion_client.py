"""Client Notion API — lecture / écriture de la base de recettes."""

import httpx
import logging
import time
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)
NOTION_VERSION = "2022-06-28"
BASE_URL = "https://api.notion.com/v1"
CACHE_TTL = 60  # secondes : durée de vie du cache de la liste des recettes


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
        self._cache: list[dict[str, Any]] | None = None
        self._cache_ts: float = 0.0

    def invalidate_cache(self) -> None:
        """Force le prochain get_all_recipes à refetch (après une écriture)."""
        self._cache = None

    # ── Récupération des recettes ──────────────────────────────────

    async def get_all_recipes(self, force: bool = False) -> list[dict[str, Any]]:
        """Liste des recettes, avec cache mémoire court (TTL) pour éviter de
        re-paginer Notion à chaque page vue / génération."""
        if not force and self._cache is not None and (time.monotonic() - self._cache_ts) < CACHE_TTL:
            return self._cache
        recipes = await self._fetch_all_recipes()
        self._cache = recipes
        self._cache_ts = time.monotonic()
        return recipes

    async def get_recipe(self, page_id: str) -> dict[str, Any] | None:
        """Récupère UNE recette par son id (1 appel, pas toute la base)."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{BASE_URL}/pages/{page_id}", headers=self._headers, timeout=30
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            recipe = self._parse_page(resp.json())
            return recipe if recipe["nom"] else None

    async def get_recipe_instructions(self, page_id: str) -> list[str]:
        """Récupère les étapes de cuisson depuis les blocks de la page."""
        steps: list[str] = []
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{BASE_URL}/blocks/{page_id}/children?page_size=100",
                headers=self._headers, timeout=30,
            )
            if resp.status_code >= 400:
                return steps
            for block in resp.json().get("results", []):
                if block.get("type") == "numbered_list_item":
                    rt = block["numbered_list_item"].get("rich_text", [])
                    txt = "".join(t.get("plain_text", "") for t in rt).strip()
                    if txt:
                        steps.append(txt)
        return steps

    async def _fetch_all_recipes(self) -> list[dict[str, Any]]:
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

        # Repas (multi_select) : cours du repas, liste de str.
        repas = [t.get("name", "") for t in (p.get("Repas", {}).get("multi_select") or []) if t.get("name")]

        # Tag (multi_select)
        tags = [t.get("name", "") for t in (p.get("Tag", {}).get("multi_select") or []) if t.get("name")]

        # Nature (select) : « Recette » par défaut si vide.
        nature = "Recette"
        sel = p.get("Nature", {}).get("select")
        if sel and sel.get("name"):
            nature = sel.get("name", "")

        # Base (multi_select) : ingrédient principal.
        base = [b.get("name", "") for b in (p.get("Base", {}).get("multi_select") or []) if b.get("name")]

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

        # Portions (number) : base de portions de la recette. Absente ou <= 0 →
        # défaut 4 (reproduit l'ancien comportement global BASE_SERVINGS).
        base_servings = 4
        portions = p.get("Portions", {}).get("number")
        try:
            if portions is not None and int(portions) > 0:
                base_servings = int(portions)
        except (TypeError, ValueError):
            base_servings = 4

        # Image = couverture de la page (external ou fichier uploadé)
        image = ""
        cover = page.get("cover")
        if cover:
            image = cover.get("external", {}).get("url", "") or cover.get("file", {}).get("url", "")

        return {
            "id": page["id"],
            "nom": nom,
            "url": url,
            "notion_url": page.get("url", ""),
            "repas": repas,
            "base": base,
            "nature": nature,
            "tags": tags,
            "note": note,
            "etat": etat,
            "moment": moment,
            "base_servings": base_servings,
            "image": image,
        }

    # ── Création d'une fiche ──────────────────────────────────────

    async def create_recipe(
        self,
        nom: str,
        url: str = "",
        repas: list[str] | str = "",
        tags: list[str] | None = None,
        etat: str = "À essayer",
        moment: str = "",
        nature: str = "",
        base: list[str] | str = "",
        base_servings: int | None = None,
    ) -> dict[str, Any]:
        """Crée une nouvelle page dans la base de recettes."""
        properties: dict[str, Any] = {
            "Nom": {"title": [{"text": {"content": nom}}]},
            # URL vide → null (Notion rejette une chaîne vide pour une propriété URL).
            "URL": {"url": url or None},
            "État": {"status": {"name": etat}},
        }
        if base_servings is not None:
            properties["Portions"] = {"number": base_servings}

        repas_names = [repas] if isinstance(repas, str) else list(repas)
        repas_names = [n for n in repas_names if n]
        if repas_names:
            properties["Repas"] = {"multi_select": [{"name": n} for n in repas_names]}
        base_names = [base] if isinstance(base, str) else list(base)
        base_names = [n for n in base_names if n]
        if base_names:
            properties["Base"] = {"multi_select": [{"name": n} for n in base_names]}
        if tags:
            properties["Tag"] = {
                "multi_select": [{"name": t} for t in tags]
            }
        if moment:
            properties["Moment"] = {"select": {"name": moment}}
        if nature:
            properties["Nature"] = {"select": {"name": nature}}

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
            self.invalidate_cache()  # la nouvelle recette doit apparaître
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

    async def rewrite_recipe_body(self, page_id: str, instructions_text: str) -> dict[str, Any]:
        """Nettoie le corps de la page (anciennes sections recette accumulées :
        Ingrédients / Préparation / Instructions, titres + listes/paragraphes)
        puis ré-écrit les instructions à jour. Évite les doublons et résidus."""
        keywords = ("ingrédient", "ingredient", "préparation", "preparation", "instruction")
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{BASE_URL}/blocks/{page_id}/children?page_size=100",
                headers=self._headers, timeout=30,
            )
            to_delete: list[str] = []
            if resp.status_code < 400:
                in_section = False
                for b in resp.json().get("results", []):
                    t = b.get("type", "")
                    if t.startswith("heading_"):
                        txt = "".join(
                            x.get("plain_text", "") for x in b.get(t, {}).get("rich_text", [])
                        ).lower()
                        in_section = any(k in txt for k in keywords)
                        if in_section:
                            to_delete.append(b["id"])
                    elif in_section and t in ("bulleted_list_item", "numbered_list_item", "paragraph"):
                        to_delete.append(b["id"])
            for bid in to_delete:
                await client.delete(f"{BASE_URL}/blocks/{bid}", headers=self._headers, timeout=30)
        return await self.append_instructions(page_id, instructions_text)

    async def update_recipe_url(self, page_id: str, url: str) -> dict[str, Any]:
        """Met à jour l'URL source d'une recette existante."""
        if not url:
            return {}
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{BASE_URL}/pages/{page_id}",
                headers=self._headers,
                json={"properties": {"URL": {"url": url}}},
                timeout=30,
            )
            resp.raise_for_status()
            self.invalidate_cache()
            return resp.json()

    async def update_portions(self, page_id: str, n: int) -> dict[str, Any]:
        """Met à jour la base de portions (propriété number « Portions »)."""
        if not n or n <= 0:
            return {}
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{BASE_URL}/pages/{page_id}",
                headers=self._headers,
                json={"properties": {"Portions": {"number": n}}},
                timeout=30,
            )
            resp.raise_for_status()
            self.invalidate_cache()
            return resp.json()

    async def update_recipe_title(self, page_id: str, nom: str) -> dict[str, Any]:
        """Met à jour le titre (Nom) d'une recette existante."""
        if not nom:
            return {}
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{BASE_URL}/pages/{page_id}",
                headers=self._headers,
                json={"properties": {"Nom": {"title": [{"text": {"content": nom}}]}}},
                timeout=30,
            )
            resp.raise_for_status()
            self.invalidate_cache()
            return resp.json()

    async def update_recipe_meta(
        self, page_id: str, repas: list[str] | str = "", tags: list[str] | None = None,
        nature: str = "", base: list[str] | str | None = None,
    ) -> dict[str, Any]:
        """Met à jour le type (Repas), les tags, la Nature et la Base d'une
        recette existante. Seuls les champs fournis sont écrits."""
        properties: dict[str, Any] = {}
        repas_names = [repas] if isinstance(repas, str) else list(repas)
        repas_names = [n for n in repas_names if n]
        if repas_names:
            properties["Repas"] = {"multi_select": [{"name": n} for n in repas_names]}
        if tags is not None:
            properties["Tag"] = {"multi_select": [{"name": t} for t in tags]}
        if base is not None:
            base_names = [base] if isinstance(base, str) else list(base)
            base_names = [n for n in base_names if n]
            properties["Base"] = {"multi_select": [{"name": n} for n in base_names]}
        if nature:
            properties["Nature"] = {"select": {"name": nature}}
        if not properties:
            return {}
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{BASE_URL}/pages/{page_id}",
                headers=self._headers, json={"properties": properties}, timeout=30,
            )
            resp.raise_for_status()
            self.invalidate_cache()
            return resp.json()


    async def archive_recipe(self, page_id: str) -> dict[str, Any]:
        """Archive (= supprime) une recette dans Notion. Notion n'a pas de vraie
        suppression via l'API : on passe la page en `archived: true`."""
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{BASE_URL}/pages/{page_id}",
                headers=self._headers,
                json={"archived": True},
                timeout=30,
            )
            resp.raise_for_status()
            self.invalidate_cache()  # la recette disparaît de la liste
            return resp.json()

    # ── Mise à jour de la note ──────────────────────────────────

    async def update_rating(
        self,
        page_id: str,
        note: str,
    ) -> dict[str, Any]:
        """Met à jour la note d'une recette (⭐ à ⭐⭐⭐⭐⭐, ou "" pour effacer)."""
        properties = {
            "Note": {
                "select": {"name": note} if note else None
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
            self.invalidate_cache()  # note à jour dans la liste
            return resp.json()

    async def update_status(self, page_id: str, etat: str) -> dict[str, Any]:
        """Met à jour l'état (propriété `status`) d'une recette."""
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{BASE_URL}/pages/{page_id}",
                headers=self._headers,
                json={"properties": {"État": {"status": {"name": etat}}}},
                timeout=30,
            )
            resp.raise_for_status()
            self.invalidate_cache()  # état à jour dans la liste
            return resp.json()
