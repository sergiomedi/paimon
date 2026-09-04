"""Who is calling, and whose material they may read."""

import pytest
from tests.unit.agents.conftest import TENANT, Harness

from paimon.agents.tools import READ_DOCUMENT, SEARCH_CORPUS
from paimon.domain.errors import InvalidTokenError
from paimon.domain.ports import ToolCall
from paimon.infrastructure.identity import DevIdentityProvider
from paimon.interfaces.mcp import McpToolGateway, bearer_token

SIGNING_KEY = "test-signing-key-padded-to-thirty-two-bytes"


def gateway(harness: Harness) -> McpToolGateway:
    return McpToolGateway(
        DevIdentityProvider(signing_key=SIGNING_KEY), harness.retrieve, harness.repository
    )


def token(tenant_id: str = TENANT) -> str:
    return DevIdentityProvider(signing_key=SIGNING_KEY).issue(subject="caller", tenant_id=tenant_id)


def auth(tenant_id: str = TENANT) -> dict[str, str]:
    return {"authorization": f"Bearer {token(tenant_id)}"}


class TestReadingTheToken:
    def test_a_bearer_header_yields_the_token(self) -> None:
        assert bearer_token({"authorization": "Bearer abc"}) == "abc"

    def test_the_header_name_is_matched_case_insensitively(self) -> None:
        assert bearer_token({"Authorization": "Bearer abc"}) == "abc"

    def test_the_scheme_is_matched_case_insensitively(self) -> None:
        assert bearer_token({"authorization": "bearer abc"}) == "abc"

    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"authorization": ""},
            {"authorization": "Bearer"},
            {"authorization": "Bearer   "},
            {"authorization": "Basic abc"},
        ],
    )
    def test_anything_else_is_refused(self, headers: dict[str, str]) -> None:
        with pytest.raises(InvalidTokenError):
            bearer_token(headers)

    def test_a_transport_with_no_headers_at_all_is_refused_clearly(self) -> None:
        # stdio carries none. This server has a tenant to establish, and a
        # transport that cannot carry a token cannot establish one.
        with pytest.raises(InvalidTokenError, match="did not carry"):
            bearer_token(None)


class TestScoping:
    async def test_the_tenant_comes_from_the_token(self) -> None:
        harness = Harness()
        principal = await gateway(harness).caller(auth("tenant-x"))
        assert principal.tenant_id == "tenant-x"

    async def test_an_executor_is_bound_to_that_tenant(self) -> None:
        harness = Harness()
        await harness.index()
        instance = gateway(harness)
        principal = await instance.caller(auth("tenant-b"))
        result = await instance.executor_for(principal).run(
            ToolCall(call_id="c", name=SEARCH_CORPUS.name, arguments={"query": "draining"})
        )
        assert "Do not answer from memory" in result

    async def test_a_tool_call_cannot_choose_its_own_tenant(self) -> None:
        # Over MCP the client is literally a model, so an executor that took its
        # tenant from a call would take it from something a prompt can talk into
        # changing.
        harness = Harness()
        await harness.index()
        result = await gateway(harness).run(
            SEARCH_CORPUS.name,
            {"query": "draining", "tenant_id": TENANT},
            call_id="c-1",
            headers=auth("tenant-b"),
        )
        assert "Do not answer from memory" in result


class TestRunning:
    async def test_a_search_returns_passages(self) -> None:
        harness = Harness()
        await harness.index()
        result = await gateway(harness).run(
            SEARCH_CORPUS.name, {"query": "draining"}, call_id="c-1", headers=auth()
        )
        assert "document: runbook" in result

    async def test_a_document_can_be_read(self) -> None:
        harness = Harness()
        await harness.index()
        result = await gateway(harness).run(
            READ_DOCUMENT.name, {"document_id": "runbook"}, call_id="c-1", headers=auth()
        )
        assert "Cordon the node first" in result

    async def test_an_unauthenticated_call_never_reaches_a_tool(self) -> None:
        harness = Harness()
        await harness.index()
        with pytest.raises(InvalidTokenError):
            await gateway(harness).run(
                SEARCH_CORPUS.name, {"query": "draining"}, call_id="c-1", headers={}
            )
