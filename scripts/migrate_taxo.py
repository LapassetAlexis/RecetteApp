"""Migration de la taxonomie Notion vers « une dimension = une propriété ».

Refonte (PR A) :
  - Nature (select)     : Recette | Ingrédient          (défaut Recette)
  - Repas (multi_select): cours du repas — on RETIRE Légume/Accompagnement
  - Base (multi_select) : ingrédient principal (Viande/Poisson/Œuf/Légume/…)
  - Moment (select)     : Midi | Soir | Les deux (inchangé)
  - Tag (multi_select)  : attributs — on RETIRE Viande/Poisson/Légumes → Base,
                          Midi/Soir → Moment, et on renomme
                          « Végétarien proténiné » → « Végétarien »

SÛRETÉ MAXIMALE :
  - Idempotent : re-run sans effet si déjà migré.
  - AUCUNE suppression d'option Notion (Légume/Accompagnement/Midi/Soir/
    « Végétarien proténiné » restent des options mortes).
  - Dry-run par défaut : logue ce qui CHANGERAIT (avant → après) + résumé.
  - `--apply` : écrit réellement via l'API, en isolant l'échec de chaque recette.

Usage :
    python -m scripts.migrate_taxo            # dry-run (aucune écriture)
    python -m scripts.migrate_taxo --apply    # applique les changements
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

import httpx

from app.config import BASE_OPTIONS, NATURE_OPTIONS
from app.notion_client import BASE_URL, NOTION_VERSION
from app.config import settings

logger = logging.getLogger("migrate_taxo")


# ── Logique PURE (testable sans réseau) ──────────────────────────────

def _dedup(seq: list[str]) -> list[str]:
    """Déduplique en conservant l'ordre."""
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def derive_changes(recette: dict[str, Any]) -> dict[str, Any]:
    """Calcule les changements de taxonomie à appliquer à UNE recette.

    Entrée : état BRUT de la page Notion (valeurs telles quelles, sans dérivation
    tolérante) : {nom, repas: list, tags: list, base: list, nature: str, moment: str}.

    Sortie : dict des SEULES propriétés qui changent (repas/tags/base/nature/
    moment). Dict vide si la recette est déjà migrée (fonction idempotente).

    Table M2 :
      - Nature = Recette si vide.
      - Tag « Viande » → Base += Viande (retire le tag) ; idem « Poisson ».
      - Tag « Légumes » OU Repas « Légume »/« Accompagnement » → Base += Légume
        (retire le tag « Légumes », retire « Légume »/« Accompagnement » de Repas ;
        ne force PAS « Plat »).
      - Tag « Midi »/« Soir » : si Moment vide → pose Moment (Les deux si les
        deux), puis retire les tags ; si Moment déjà rempli → retire juste les tags.
      - Tag « Végétarien proténiné » → « Végétarien ».
      - Base = multi-valeurs si plusieurs signaux ; laissée vide si aucun.
    """
    tags = list(recette.get("tags") or [])
    repas = list(recette.get("repas") or [])
    base = list(recette.get("base") or [])
    nature = recette.get("nature") or ""
    moment = recette.get("moment") or ""

    new_tags = list(tags)
    new_repas = list(repas)
    new_base = list(base)
    new_moment = moment
    new_nature = nature or "Recette"

    def drop(lst: list[str], val: str) -> None:
        while val in lst:
            lst.remove(val)

    # Base depuis les signaux hérités.
    if "Viande" in tags:
        new_base.append("Viande")
        drop(new_tags, "Viande")
    if "Poisson" in tags:
        new_base.append("Poisson")
        drop(new_tags, "Poisson")
    if "Légumes" in tags or "Légume" in repas or "Accompagnement" in repas:
        new_base.append("Légume")
        drop(new_tags, "Légumes")
        drop(new_repas, "Légume")
        drop(new_repas, "Accompagnement")

    # Moment depuis les tags Midi/Soir.
    has_midi = "Midi" in tags
    has_soir = "Soir" in tags
    if has_midi or has_soir:
        if not moment:
            if has_midi and has_soir:
                new_moment = "Les deux"
            elif has_midi:
                new_moment = "Midi"
            else:
                new_moment = "Soir"
        drop(new_tags, "Midi")
        drop(new_tags, "Soir")

    # Renommage du tag régime.
    if "Végétarien proténiné" in new_tags:
        new_tags = ["Végétarien" if t == "Végétarien proténiné" else t for t in new_tags]

    new_tags = _dedup(new_tags)
    new_repas = _dedup(new_repas)
    new_base = _dedup(new_base)

    changes: dict[str, Any] = {}
    if new_nature != nature:  # uniquement si Nature était vide
        changes["nature"] = new_nature
    if new_repas != repas:
        changes["repas"] = new_repas
    if new_tags != tags:
        changes["tags"] = new_tags
    if new_base != base:
        changes["base"] = new_base
    if new_moment != moment:
        changes["moment"] = new_moment
    return changes


def _changes_to_properties(changes: dict[str, Any]) -> dict[str, Any]:
    """Traduit un dict de changements en payload `properties` Notion."""
    props: dict[str, Any] = {}
    if "nature" in changes:
        props["Nature"] = {"select": {"name": changes["nature"]}}
    if "repas" in changes:
        props["Repas"] = {"multi_select": [{"name": n} for n in changes["repas"]]}
    if "tags" in changes:
        props["Tag"] = {"multi_select": [{"name": t} for t in changes["tags"]]}
    if "base" in changes:
        props["Base"] = {"multi_select": [{"name": b} for b in changes["base"]]}
    if "moment" in changes:
        props["Moment"] = {"select": {"name": changes["moment"]}}
    return props


# ── Lecture BRUTE des pages (sans dérivation tolérante) ───────────────

def _raw_page(page: dict[str, Any]) -> dict[str, Any]:
    """Extrait l'état BRUT d'une page Notion (pas de dérivation de Base)."""
    p = page.get("properties", {})

    title = p.get("Nom", {}).get("title") or []
    nom = title[0].get("plain_text", "") if title else ""

    repas_prop = p.get("Repas", {})
    multi = repas_prop.get("multi_select")
    if multi is not None:
        repas = [t.get("name", "") for t in multi if t.get("name")]
    else:
        sel = repas_prop.get("select")
        repas = [sel.get("name", "")] if sel and sel.get("name") else []

    tags = [t.get("name", "") for t in (p.get("Tag", {}).get("multi_select") or []) if t.get("name")]
    base = [b.get("name", "") for b in (p.get("Base", {}).get("multi_select") or []) if b.get("name")]

    nature = ""
    sel = p.get("Nature", {}).get("select")
    if sel:
        nature = sel.get("name", "")

    moment = ""
    sel = p.get("Moment", {}).get("select")
    if sel:
        moment = sel.get("name", "")

    return {
        "id": page["id"],
        "nom": nom,
        "repas": repas,
        "tags": tags,
        "base": base,
        "nature": nature,
        "moment": moment,
    }


# ── Réseau ───────────────────────────────────────────────────────────

def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.notion_token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


async def _fetch_raw_pages(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Parcourt toute la base et renvoie les pages à l'état brut."""
    out: list[dict[str, Any]] = []
    start_cursor: str | None = None
    while True:
        body: dict[str, Any] = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor
        resp = await client.post(
            f"{BASE_URL}/databases/{settings.notion_database_id}/query",
            headers=_headers(), json=body, timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for page in data.get("results", []):
            raw = _raw_page(page)
            if raw["nom"]:
                out.append(raw)
        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")
    return out


async def _ensure_schema(client: httpx.AsyncClient, apply: bool) -> None:
    """Crée les propriétés Nature/Base si absentes et ajoute l'option Tag
    « Végétarien ». Ne supprime jamais d'options existantes."""
    resp = await client.get(
        f"{BASE_URL}/databases/{settings.notion_database_id}",
        headers=_headers(), timeout=30,
    )
    resp.raise_for_status()
    props = resp.json().get("properties", {})

    update: dict[str, Any] = {}

    if "Nature" not in props:
        update["Nature"] = {"select": {"options": [{"name": n} for n in NATURE_OPTIONS]}}
        logger.info("Schéma : créer la propriété « Nature » (select : %s)", NATURE_OPTIONS)

    if "Base" not in props:
        update["Base"] = {"multi_select": {"options": [{"name": b} for b in BASE_OPTIONS]}}
        logger.info("Schéma : créer la propriété « Base » (multi_select : %s)", BASE_OPTIONS)

    # Tag : ajouter l'option « Végétarien » sans retirer les existantes.
    tag_prop = props.get("Tag", {})
    tag_ms = tag_prop.get("multi_select")
    if tag_ms is not None:
        existing = tag_ms.get("options", [])
        names = {o.get("name") for o in existing}
        if "Végétarien" not in names:
            # On renvoie les options existantes TELLES QUELLES (id + couleur
            # préservés) + la nouvelle, pour ne pas réattribuer les couleurs.
            merged = list(existing) + [{"name": "Végétarien"}]
            update["Tag"] = {"multi_select": {"options": merged}}
            logger.info("Schéma : ajouter l'option Tag « Végétarien » (options existantes préservées)")

    if not update:
        logger.info("Schéma : déjà à jour, rien à créer.")
        return

    if not apply:
        logger.info("[dry-run] Schéma : %d propriété(s) seraient créées/modifiées : %s",
                    len(update), list(update.keys()))
        return

    patch = await client.patch(
        f"{BASE_URL}/databases/{settings.notion_database_id}",
        headers=_headers(), json={"properties": update}, timeout=30,
    )
    patch.raise_for_status()
    logger.info("Schéma mis à jour : %s", list(update.keys()))


def _fmt(changes: dict[str, Any], raw: dict[str, Any]) -> str:
    """Formatte un « avant → après » lisible pour le log."""
    parts = []
    for key in ("nature", "repas", "tags", "base", "moment"):
        if key in changes:
            parts.append(f"{key}: {raw.get(key)!r} → {changes[key]!r}")
    return " | ".join(parts)


async def _migrate_data(client: httpx.AsyncClient, apply: bool) -> dict[str, int]:
    """Applique la table M2 à chaque recette. Renvoie des compteurs."""
    pages = await _fetch_raw_pages(client)
    counters = {"total": len(pages), "changed": 0, "unchanged": 0, "errors": 0}
    for raw in pages:
        try:
            changes = derive_changes(raw)
        except Exception:
            logger.exception("Calcul des changements échoué pour « %s »", raw.get("nom"))
            counters["errors"] += 1
            continue
        if not changes:
            counters["unchanged"] += 1
            continue
        counters["changed"] += 1
        prefix = "[dry-run]" if not apply else "[apply]"
        logger.info("%s « %s » — %s", prefix, raw["nom"], _fmt(changes, raw))
        if not apply:
            continue
        try:
            resp = await client.patch(
                f"{BASE_URL}/pages/{raw['id']}",
                headers=_headers(),
                json={"properties": _changes_to_properties(changes)},
                timeout=30,
            )
            resp.raise_for_status()
        except Exception:
            logger.exception("Écriture Notion échouée pour « %s »", raw["nom"])
            counters["errors"] += 1
            counters["changed"] -= 1
    return counters


# Options devenues mortes après migration data (plus portées par aucune recette).
# `--cleanup` les retire de la base Notion.
_DEAD_OPTIONS: dict[str, list[str]] = {
    "Repas": ["Légume", "Accompagnement"],
    "Tag": ["Midi", "Soir", "Végétarien proténiné"],
}


async def _cleanup_schema(client: httpx.AsyncClient, apply: bool) -> None:
    """Supprime les options mortes (post-migration) sans toucher aux autres.

    À ne lancer QU'APRÈS la migration data (--apply), quand plus aucune recette
    ne porte ces valeurs."""
    resp = await client.get(
        f"{BASE_URL}/databases/{settings.notion_database_id}",
        headers=_headers(), timeout=30,
    )
    resp.raise_for_status()
    props = resp.json().get("properties", {})

    update: dict[str, Any] = {}
    for prop_name, dead in _DEAD_OPTIONS.items():
        ms = props.get(prop_name, {}).get("multi_select")
        if not ms:
            continue
        options = ms.get("options", [])
        removed = [o["name"] for o in options if o.get("name") in dead]
        if removed:
            kept = [o for o in options if o.get("name") not in dead]  # préserve id/couleur
            update[prop_name] = {"multi_select": {"options": kept}}
            logger.info("Nettoyage %s : retire %s", prop_name, removed)

    if not update:
        logger.info("Nettoyage : aucune option morte à retirer.")
        return
    if not apply:
        logger.info("[dry-run] Nettoyage : %s propriété(s) seraient modifiées : %s",
                    len(update), list(update.keys()))
        return
    patch = await client.patch(
        f"{BASE_URL}/databases/{settings.notion_database_id}",
        headers=_headers(), json={"properties": update}, timeout=30,
    )
    patch.raise_for_status()
    logger.info("Options mortes supprimées : %s", list(update.keys()))


async def run(apply: bool, cleanup: bool = False) -> None:
    if not settings.notion_token:
        raise SystemExit("NOTION_TOKEN manquant : configure l'environnement avant de lancer la migration.")
    mode = "APPLY (écriture réelle)" if apply else "DRY-RUN (aucune écriture)"
    if cleanup:
        logger.info("=== Nettoyage options mortes — %s ===", mode)
        async with httpx.AsyncClient() as client:
            await _cleanup_schema(client, apply)
        return
    logger.info("=== Migration taxonomie — %s ===", mode)
    async with httpx.AsyncClient() as client:
        await _ensure_schema(client, apply)
        counters = await _migrate_data(client, apply)
    logger.info(
        "=== Résumé : %d recettes, %d à changer, %d inchangées, %d erreurs ===",
        counters["total"], counters["changed"], counters["unchanged"], counters["errors"],
    )
    if not apply and counters["changed"]:
        logger.info("Dry-run terminé. Relance avec --apply pour écrire ces changements.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Migration de la taxonomie Notion (PR A).")
    parser.add_argument("--apply", action="store_true",
                        help="Applique réellement les changements (sinon dry-run).")
    parser.add_argument("--cleanup", action="store_true",
                        help="Supprime les options mortes post-migration (au lieu de migrer).")
    args = parser.parse_args()
    asyncio.run(run(args.apply, args.cleanup))


if __name__ == "__main__":
    main()
