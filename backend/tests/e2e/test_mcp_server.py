"""The MCP server, spoken to by a real MCP client.

Over HTTP against the mounted application rather than by calling the tool
functions directly: the protocol, the mount, the sub-application's lifespan and
the authentication path are what is new here, and none of them exist until an
actual client connects.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from paimon.agents.tools import READ_DOCUMENT, SEARCH_CORPUS
from paimon.application.use_cases import RetrieveChunks
from paimon.infrastructure.identity import DevIdentityProvider
from paimon.interfaces.mcp import McpToolGateway
from tests.conftest import DEV_SIGNING_KEY
from tests.e2e.test_agents_api import TENANT, Backend

# "localhost" rather than the usual test host: the transport rejects a Host it
# was not configured for, which is its DNS-rebinding protection, and the test
# should exercise the hosts the platform actually ships with rather than widen
# them for its own convenience.
MCP_URL = "http://localhost/mcp/"


def token_for(_backend: Backend, tenant_id: str = TENANT) -> str:
    """A token for the corpus's tenant."""
    return DevIdentityProvider(signing_key=DEV_SIGNING_KEY).issue(
        subject="caller", tenant_id=tenant_id
    )


def gateway_for(backend: Backend) -> McpToolGateway:
    """A gateway over the in-memory corpus, with the test signing key."""
    return McpToolGateway(
        DevIdentityProvider(signing_key=DEV_SIGNING_KEY),
        RetrieveChunks(backend.store, backend.embedding_model),
        backend.repository,
    )


def connect(app: FastAPI, token: str | None) -> tuple[httpx2.AsyncClient, str]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return (
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://localhost",
            headers=headers,
        ),
        MCP_URL,
    )


@pytest.fixture
async def corpus(app: FastAPI) -> AsyncIterator[Backend]:
    """A running application whose MCP surface reads an in-memory corpus."""
    instance = Backend()
    await instance.index()
    async with LifespanManager(app):
        app.state.mcp_gateway = lambda: gateway_for(instance)
        yield instance


@asynccontextmanager
async def session(app: FastAPI, token: str | None) -> AsyncIterator[Client]:
    """Open an MCP session against the application.

    A context manager used inside each test rather than an async fixture: an
    async generator fixture is torn down in a different task from the one that
    set it up, and the transport's cancel scopes refuse to be exited from
    another task. The failure looks like a protocol bug and is a fixture bug.
    """
    http, url = connect(app, token)
    async with (
        http,
        Client(streamable_http_client(url, http_client=http), raise_exceptions=True) as client,
    ):
        yield client


class TestDiscovery:
    async def test_it_offers_the_platform_tools(self, app: FastAPI, corpus: Backend) -> None:
        async with session(app, token_for(corpus)) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
        assert names == {SEARCH_CORPUS.name, READ_DOCUMENT.name}

    async def test_the_descriptions_are_the_ones_the_platform_declares(
        self, app: FastAPI, corpus: Backend
    ) -> None:
        # One definition, read by a model provider, by the executor and by MCP.
        async with session(app, token_for(corpus)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert tools[SEARCH_CORPUS.name].description == SEARCH_CORPUS.description
        assert tools[READ_DOCUMENT.name].description == READ_DOCUMENT.description

    async def test_a_tool_declares_its_arguments(self, app: FastAPI, corpus: Backend) -> None:
        async with session(app, token_for(corpus)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        schema = tools[SEARCH_CORPUS.name].input_schema
        assert "query" in schema["properties"]
        assert schema["required"] == ["query"]


class TestCallingTools:
    async def test_a_search_returns_passages_from_the_corpus(
        self, app: FastAPI, corpus: Backend
    ) -> None:
        async with session(app, token_for(corpus)) as client:
            result = await client.call_tool(SEARCH_CORPUS.name, {"query": "draining"})
        assert "document: runbook" in str(result.content[0].text)  # type: ignore[union-attr]

    async def test_a_document_can_be_read_whole(self, app: FastAPI, corpus: Backend) -> None:
        async with session(app, token_for(corpus)) as client:
            result = await client.call_tool(READ_DOCUMENT.name, {"document_id": "runbook"})
        assert "Cordon the node first" in str(result.content[0].text)  # type: ignore[union-attr]

    async def test_an_unknown_document_is_reported_to_the_model_not_raised(
        self, app: FastAPI, corpus: Backend
    ) -> None:
        # The client here is a model. A message it can act on beats a traceback
        # it will paraphrase into a claim about the corpus.
        async with session(app, token_for(corpus)) as client:
            result = await client.call_tool(READ_DOCUMENT.name, {"document_id": "nothing"})
        assert "No document 'nothing'" in str(result.content[0].text)  # type: ignore[union-attr]

    async def test_a_blank_query_is_reported_as_an_error_message(
        self, app: FastAPI, corpus: Backend
    ) -> None:
        async with session(app, token_for(corpus)) as client:
            result = await client.call_tool(SEARCH_CORPUS.name, {"query": "   "})
        assert "error:" in str(result.content[0].text)  # type: ignore[union-attr]


class TestAuthentication:
    async def test_a_call_without_a_token_reaches_no_tool(
        self, app: FastAPI, corpus: Backend
    ) -> None:
        async with session(app, None) as client:
            result = await client.call_tool(SEARCH_CORPUS.name, {"query": "draining"})
        assert "error:" in str(result.content[0].text)  # type: ignore[union-attr]

    async def test_another_tenant_sees_nothing_of_this_ones_corpus(
        self, app: FastAPI, corpus: Backend, dev_identity_provider: DevIdentityProvider
    ) -> None:
        token = dev_identity_provider.issue(subject="other", tenant_id="tenant-2")
        async with session(app, token) as client:
            result = await client.call_tool(SEARCH_CORPUS.name, {"query": "draining"})
        assert "Do not answer from memory" in str(result.content[0].text)  # type: ignore[union-attr]
