"""The MCP endpoint as an OAuth 2.1 resource server.

What the protocol requires beyond "reject bad tokens": an unauthenticated caller
must be told *where* to get a good one, and the document it is sent to must exist
at the address the specification puts it at.
"""

import json
from collections.abc import AsyncIterator

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from pydantic import SecretStr

from paimon import __version__
from paimon.config import (
    AuthSettings,
    DatabaseSettings,
    Environment,
    McpSettings,
    ObservabilitySettings,
    RedisSettings,
    Settings,
)
from paimon.infrastructure.identity import DevIdentityProvider
from paimon.interfaces.api import create_app
from paimon.interfaces.mcp.discovery import (
    DISCOVERY_PATH,
    SCHEMA_URL,
    SERVER_ID,
    server_json,
)
from tests.conftest import DEV_SIGNING_KEY

RESOURCE_URL = "http://localhost/mcp"
AUTHORIZATION_SERVER = "https://login.example.test/tenant"
METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"


def protected_settings() -> Settings:
    """Settings for a deployment that publishes protected resource metadata."""
    return Settings(
        _env_file=None,
        environment=Environment.TEST,
        database=DatabaseSettings(
            host="localhost",
            user="paimon",
            password=SecretStr("test"),
            name="paimon_test",
        ),
        redis=RedisSettings(host="localhost"),
        auth=AuthSettings(provider="dev", dev_signing_key=SecretStr(DEV_SIGNING_KEY)),
        observability=ObservabilitySettings(log_format="console", log_level="WARNING"),
        mcp=McpSettings(resource_url=RESOURCE_URL, authorization_server=AUTHORIZATION_SERVER),
    )


@pytest.fixture
async def protected() -> AsyncIterator[httpx.AsyncClient]:
    """A running application whose MCP endpoint is a protected resource."""
    app: FastAPI = create_app(protected_settings())
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            yield client


def token(tenant_id: str = "tenant-1") -> str:
    return DevIdentityProvider(signing_key=DEV_SIGNING_KEY).issue(
        subject="caller", tenant_id=tenant_id
    )


class TestTheChallenge:
    async def test_an_anonymous_request_is_refused_with_401(
        self, protected: httpx.AsyncClient
    ) -> None:
        response = await protected.post("/mcp/", json={})
        assert response.status_code == 401

    async def test_the_challenge_says_where_to_get_a_token(
        self, protected: httpx.AsyncClient
    ) -> None:
        # Without resource_metadata a 401 tells a client it is unwelcome. With
        # it, the client can discover the authorization server and come back.
        response = await protected.post("/mcp/", json={})
        header = response.headers["WWW-Authenticate"]
        assert header.startswith("Bearer ")
        assert f'resource_metadata="http://localhost{METADATA_PATH}"' in header

    async def test_a_malformed_authorization_header_is_refused_the_same_way(
        self, protected: httpx.AsyncClient
    ) -> None:
        response = await protected.post("/mcp/", json={}, headers={"Authorization": "Basic abc"})
        assert response.status_code == 401
        assert "resource_metadata" in response.headers["WWW-Authenticate"]

    async def test_a_token_from_another_signer_is_refused(
        self, protected: httpx.AsyncClient
    ) -> None:
        other = DevIdentityProvider(signing_key="a-different-key-also-thirty-two-bytes")
        response = await protected.post(
            "/mcp/",
            json={},
            headers={"Authorization": f"Bearer {other.issue(subject='x', tenant_id='t')}"},
        )
        assert response.status_code == 401


class TestTheMetadataDocument:
    async def test_it_is_served_where_the_specification_puts_it(
        self, protected: httpx.AsyncClient
    ) -> None:
        # At the root with the resource path appended — not under the path the
        # server happens to be mounted at, which is where a client would not
        # look.
        assert (await protected.get(METADATA_PATH)).status_code == 200

    async def test_it_names_this_server_as_the_resource(self, protected: httpx.AsyncClient) -> None:
        # The audience a client will ask its authorization server for. If this
        # were the container's address rather than the public one, every token
        # would be minted for an audience this server does not accept.
        body = json.loads((await protected.get(METADATA_PATH)).text)
        assert body["resource"] == RESOURCE_URL

    async def test_it_names_the_authorization_server(self, protected: httpx.AsyncClient) -> None:
        body = json.loads((await protected.get(METADATA_PATH)).text)
        assert AUTHORIZATION_SERVER in [
            server.rstrip("/") for server in body["authorization_servers"]
        ]

    async def test_it_needs_no_token_of_its_own(self, protected: httpx.AsyncClient) -> None:
        # A discovery document behind authentication cannot be discovered.
        response = await protected.get(METADATA_PATH)
        assert response.status_code == 200
        assert "WWW-Authenticate" not in response.headers


class TestWhenAuthorizationIsNotConfigured:
    async def test_no_metadata_is_published(self, client: httpx.AsyncClient) -> None:
        # The default deployment uses the dev identity provider, which is not an
        # OAuth issuer. Advertising one that does not exist would send clients
        # somewhere useless, so nothing is advertised.
        assert (await client.get(METADATA_PATH)).status_code == 404


class TestTheDiscoveryDocument:
    """``server.json`` — how something that has never met this server finds it."""

    async def test_it_describes_the_endpoint_a_client_should_dial(
        self, protected: httpx.AsyncClient
    ) -> None:
        body = json.loads((await protected.get(DISCOVERY_PATH)).text)
        remote = body["remotes"][0]
        assert remote["type"] == "streamable-http"
        assert remote["url"] == RESOURCE_URL

    async def test_it_claims_the_schema_it_satisfies(self, protected: httpx.AsyncClient) -> None:
        # Versioned and explicit, so a registry validates it rather than guesses.
        body = json.loads((await protected.get(DISCOVERY_PATH)).text)
        assert body["$schema"] == SCHEMA_URL
        assert body["name"] == SERVER_ID
        assert body["version"] == __version__

    async def test_it_says_a_token_is_required(self, protected: httpx.AsyncClient) -> None:
        body = json.loads((await protected.get(DISCOVERY_PATH)).text)
        header = body["remotes"][0]["headers"][0]
        assert header["name"] == "Authorization"
        assert header["isRequired"] is True
        assert header["isSecret"] is True
        # And where to get one, which is the other document.
        assert "oauth-protected-resource" in header["description"]

    async def test_it_needs_no_token_of_its_own(self, protected: httpx.AsyncClient) -> None:
        # Requiring a token to learn where to send a token is a loop.
        response = await protected.get(DISCOVERY_PATH)
        assert response.status_code == 200
        assert "WWW-Authenticate" not in response.headers

    async def test_it_tells_a_browser_not_to_guess_what_it_is(
        self, protected: httpx.AsyncClient
    ) -> None:
        response = await protected.get(DISCOVERY_PATH)
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    async def test_it_lists_no_tools_no_tenants_and_no_corpus(
        self, protected: httpx.AsyncClient
    ) -> None:
        # Unauthenticated by necessity, so it says what a stranger may know: the
        # address and the header. Not what is indexed, and not who indexed it.
        body = json.loads((await protected.get(DISCOVERY_PATH)).text)
        assert set(body) == {"$schema", "name", "title", "description", "version", "remotes"}

    async def test_an_unauthenticated_deployment_says_no_header_is_needed(self) -> None:
        # Truthfully: without an authorization server there is nowhere to get a
        # token, and asking for one would be describing a door with no key.
        document = server_json(resource_url=RESOURCE_URL, version="1.2.3", authenticated=False)
        assert "headers" not in document["remotes"][0]


class TestWhenTheCanonicalUrlIsUnset:
    async def test_nothing_is_published(self, client: httpx.AsyncClient) -> None:
        # A discovery document naming a container's internal address would send
        # every client that read it somewhere unreachable — worse than silence.
        assert (await client.get(DISCOVERY_PATH)).status_code == 404
