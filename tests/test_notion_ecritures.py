"""L11 — corps des requêtes envoyées à Notion (httpx.MockTransport, sans
réseau) : ingrédients découpés sans troncature, icône préservée, Moment /
État / tags, lecture paginée des instructions."""

import asyncio
import json

import httpx

from app.notion_client import NotionClient, _rich_text_chunks


def _run(handler, fn):
    """Exécute `fn(client)` avec un client Notion branché sur `handler`.
    Renvoie (résultat, requêtes reçues)."""
    reqs: list[httpx.Request] = []

    def _h(request: httpx.Request) -> httpx.Response:
        reqs.append(request)
        return handler(request)

    async def go():
        nc = NotionClient()
        nc._client = httpx.AsyncClient(transport=httpx.MockTransport(_h))
        nc._client_loop = asyncio.get_running_loop()
        try:
            return await fn(nc)
        finally:
            await nc.aclose()

    return asyncio.run(go()), reqs


def _body(req: httpx.Request) -> dict:
    return json.loads(req.content or b"{}")


def _patches(reqs):
    return [r for r in reqs if r.method == "PATCH"]


# ── Ingrédients ──────────────────────────────────────────────────────────


def test_rich_text_chunks():
    assert _rich_text_chunks("") == [{"text": {"content": ""}}]
    chunks = _rich_text_chunks("a" * 4500)
    assert [len(c["text"]["content"]) for c in chunks] == [2000, 2000, 500]


def test_update_ingredients_texte_long_non_tronque_et_icone_posee():
    texte = "- 200 g farine\n" * 300   # 4 500 caractères

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json={"id": "p1", "icon": None})
        return httpx.Response(200, json={"id": "p1"})
    _, reqs = _run(handler, lambda nc: nc.update_ingredients("p1", texte))
    (patch,) = _patches(reqs)
    body = _body(patch)
    segs = body["properties"]["Ingrédients"]["rich_text"]
    assert "".join(s["text"]["content"] for s in segs) == texte
    assert all(len(s["text"]["content"]) <= 2000 for s in segs) and len(segs) == 3
    assert body["icon"] == {"emoji": "🥗"}      # page sans icône → 🥗


def test_update_ingredients_garde_l_icone_existante():
    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json={"id": "p1", "icon": {"emoji": "🍰"}})
        return httpx.Response(200, json={})
    _, reqs = _run(handler, lambda nc: nc.update_ingredients("p1", "- sel"))
    assert "icon" not in _body(_patches(reqs)[0])


def test_update_ingredients_lecture_icone_impossible_n_ecrase_rien():
    def handler(req):
        if req.method == "GET":
            return httpx.Response(500)
        return httpx.Response(200, json={})
    _, reqs = _run(handler, lambda nc: nc.update_ingredients("p1", "- sel"))
    assert "icon" not in _body(_patches(reqs)[0])


def test_update_ingredients_erreur_reseau_sur_l_icone():
    def handler(req):
        if req.method == "GET":
            raise httpx.ConnectError("coupé")
        return httpx.Response(200, json={})
    _, reqs = _run(handler, lambda nc: nc.update_ingredients("p1", "- sel"))
    assert "icon" not in _body(_patches(reqs)[0])


def test_update_ingredients_sans_propriete_ecrit_des_blocs():
    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json={"icon": {"emoji": "🍰"}})
        if req.url.path.endswith("/children"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(400, json={"message": "Ingrédients is not a property"})
    res, reqs = _run(handler, lambda nc: nc.update_ingredients("p1", "- sel\n- poivre"))
    assert res == {"ok": True}
    blocks = _body(_patches(reqs)[-1])["children"]
    assert [b["type"] for b in blocks] == ["heading_3", "bulleted_list_item", "bulleted_list_item"]


# ── Moment / État / tags ─────────────────────────────────────────────────


def _meta(**kw):
    _, reqs = _run(lambda req: httpx.Response(200, json={}), lambda nc: nc.update_recipe_meta("p1", **kw))
    return _body(reqs[0])["properties"] if reqs else None


def test_update_recipe_meta_vide_les_tags():
    assert _meta(tags=[]) == {"Tag": {"multi_select": []}}


def test_update_recipe_meta_moment_et_etat():
    props = _meta(repas=["Plat"], moment="Soir", etat="Testée", nature="Recette", base=["Viande"])
    assert props["Moment"] == {"select": {"name": "Soir"}}
    assert props["État"] == {"status": {"name": "Testée"}}
    assert props["Repas"] == {"multi_select": [{"name": "Plat"}]}
    assert props["Base"] == {"multi_select": [{"name": "Viande"}]}


def test_update_recipe_meta_efface_le_moment_et_ignore_un_etat_vide():
    props = _meta(moment="", etat="")
    assert props == {"Moment": {"select": None}}


def test_update_recipe_meta_rien_a_ecrire():
    assert _meta() is None


# ── Instructions : pagination et paragraphes ─────────────────────────────


def _block(t, txt):
    return {"id": txt, "type": t, t: {"rich_text": [{"plain_text": txt}]}}


def test_get_recipe_instructions_pagine_et_accepte_les_paragraphes():
    page1 = [_block("heading_3", "📝 Ingrédients"), _block("bulleted_list_item", "farine"),
             _block("paragraph", "sel"), _block("heading_3", "👨‍🍳 Instructions")]
    page1 += [_block("numbered_list_item", f"Étape {i}") for i in range(1, 97)]
    page2 = [_block("paragraph", "Étape 97"), _block("paragraph", ""), _block("image", "x"),
             _block("numbered_list_item", "Étape 98")]

    def handler(req):
        if "start_cursor=c2" in str(req.url):
            return httpx.Response(200, json={"results": page2, "has_more": False})
        return httpx.Response(200, json={"results": page1, "has_more": True, "next_cursor": "c2"})
    steps, reqs = _run(handler, lambda nc: nc.get_recipe_instructions("p1"))
    assert len(reqs) == 2
    assert steps[0] == "Étape 1" and steps[-1] == "Étape 98" and len(steps) == 98
    assert "farine" not in steps and "sel" not in steps


def test_get_recipe_instructions_page_illisible():
    steps, _ = _run(lambda req: httpx.Response(404), lambda nc: nc.get_recipe_instructions("p1"))
    assert steps == []


def test_rewrite_recipe_body_supprime_les_sections_de_toutes_les_pages():
    page1 = [_block("heading_2", "Ingrédients"), _block("bulleted_list_item", "a")]
    page2 = [_block("heading_2", "Préparation"), _block("paragraph", "b"),
             _block("heading_2", "Notes"), _block("paragraph", "garder")]

    def handler(req):
        if req.method == "GET":
            if "start_cursor=c2" in str(req.url):
                return httpx.Response(200, json={"results": page2, "has_more": False})
            return httpx.Response(200, json={"results": page1, "has_more": True, "next_cursor": "c2"})
        return httpx.Response(200, json={})
    _, reqs = _run(handler, lambda nc: nc.rewrite_recipe_body("p1", "Cuire."))
    deleted = {r.url.path.rsplit("/", 1)[1] for r in reqs if r.method == "DELETE"}
    assert deleted == {"Ingrédients", "a", "Préparation", "b"}
    appended = _body(_patches(reqs)[-1])["children"]
    assert appended[-1]["numbered_list_item"]["rich_text"][0]["text"]["content"] == "Cuire."
