"""Client HTTP partagé du client Notion : une instance par boucle, fermée par
`aclose()` (appelé dans le lifespan de l'app)."""

import asyncio

from app.notion_client import NotionClient


def test_client_partage_dans_une_meme_boucle():
    n = NotionClient()

    async def run():
        a, b = n._http(), n._http()
        assert a is b and not a.is_closed
        await n.aclose()
        assert a.is_closed
        c = n._http()  # recréé à la demande après fermeture
        assert c is not a
        await n.aclose()

    asyncio.run(run())


def test_client_recree_dans_une_autre_boucle():
    n = NotionClient()

    async def first():
        return n._http()

    async def second():
        c = n._http()
        await n.aclose()
        return c

    a = asyncio.run(first())
    b = asyncio.run(second())
    assert a is not b  # un client httpx ne survit pas à sa boucle


def test_aclose_sans_client_ne_fait_rien():
    asyncio.run(NotionClient().aclose())
