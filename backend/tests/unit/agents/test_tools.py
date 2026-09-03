"""The tools the platform offers a model, and what running them does."""

import pytest
from tests.unit.agents.conftest import TENANT, Harness

from paimon.agents.tools import (
    MAX_DOCUMENT_CHARACTERS,
    READ_DOCUMENT,
    SEARCH_CORPUS,
    TOOLS,
    ToolArgumentError,
    ToolExecutor,
    UnknownToolError,
)
from paimon.domain.entities import Document
from paimon.domain.ports import ToolCall, ToolDefinition


def call(name: str, **arguments: object) -> ToolCall:
    return ToolCall(call_id="c-1", name=name, arguments=arguments)


async def executor(harness: Harness) -> ToolExecutor:
    return ToolExecutor(harness.retrieve, harness.repository, TENANT)


class TestTheDeclarations:
    def test_every_tool_describes_itself(self) -> None:
        # A model picks a tool by reading its description. An undescribed tool is
        # either never chosen or chosen at random.
        assert all(tool.description.strip() for tool in TOOLS)

    def test_a_tool_without_a_description_is_refused(self) -> None:
        with pytest.raises(ValueError, match="needs a description"):
            ToolDefinition(name="mystery", description="  ", parameters={})

    def test_an_unnamed_tool_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must be named"):
            ToolDefinition(name=" ", description="does a thing", parameters={})

    def test_the_surface_stays_small(self) -> None:
        # Every tool added is one every call has to be given a reason not to
        # choose. Growing this deserves an argument, so it gets a test.
        assert len(TOOLS) == 2


class TestSearching:
    async def test_it_returns_passages_with_their_document(self) -> None:
        harness = Harness()
        await harness.index()
        result = await (await executor(harness)).run(call(SEARCH_CORPUS.name, query="draining"))
        assert "document: runbook" in result

    async def test_an_empty_corpus_says_so_and_says_not_to_guess(self) -> None:
        # A model given an empty result and no instruction fills the silence
        # from its own memory, which is the failure this platform exists to
        # avoid.
        harness = Harness()
        result = await (await executor(harness)).run(call(SEARCH_CORPUS.name, query="draining"))
        assert "Do not answer from memory" in result

    async def test_a_blank_query_is_refused(self) -> None:
        harness = Harness()
        with pytest.raises(ToolArgumentError, match="non-empty query"):
            await (await executor(harness)).run(call(SEARCH_CORPUS.name, query="   "))

    async def test_an_oversized_limit_is_clamped_not_rejected(self) -> None:
        # A model asking for fifty passages has misjudged, not malfunctioned.
        harness = Harness()
        await harness.index()
        result = await (await executor(harness)).run(
            call(SEARCH_CORPUS.name, query="draining", limit=500)
        )
        assert result

    async def test_a_limit_that_is_not_a_number_is_refused(self) -> None:
        harness = Harness()
        with pytest.raises(ToolArgumentError, match="whole number"):
            await (await executor(harness)).run(
                call(SEARCH_CORPUS.name, query="draining", limit="lots")
            )

    async def test_a_model_cannot_choose_the_tenant_it_searches(self) -> None:
        # The tenant is bound at construction and never read from a call. A
        # prompt is not a security boundary.
        harness = Harness()
        await harness.index()
        other = ToolExecutor(harness.retrieve, harness.repository, "tenant-b")
        result = await other.run(call(SEARCH_CORPUS.name, query="draining", tenant_id=TENANT))
        assert "Do not answer from memory" in result


class TestReading:
    async def test_it_returns_the_whole_document(self) -> None:
        harness = Harness()
        await harness.index()
        result = await (await executor(harness)).run(
            call(READ_DOCUMENT.name, document_id="runbook")
        )
        assert "Cordon the node first" in result

    async def test_an_unknown_document_is_reported_not_raised(self) -> None:
        harness = Harness()
        result = await (await executor(harness)).run(
            call(READ_DOCUMENT.name, document_id="nothing")
        )
        assert "No document 'nothing'" in result

    async def test_a_long_document_is_truncated_and_says_so(self) -> None:
        # A model that cannot tell it received part of a procedure will describe
        # the part it got as the whole of it.
        harness = Harness()
        long_text = "step. " * (MAX_DOCUMENT_CHARACTERS // 2)
        await harness.repository.save(
            Document(
                document_id="long",
                tenant_id=TENANT,
                source_uri="https://example.test/long",
                title="Long",
                text=long_text,
                content_hash="hash-long",
                media_type="text/markdown",
            )
        )
        result = await (await executor(harness)).run(call(READ_DOCUMENT.name, document_id="long"))
        assert result.endswith("[truncated: document continues beyond this point]")

    async def test_a_blank_identifier_is_refused(self) -> None:
        harness = Harness()
        with pytest.raises(ToolArgumentError, match="needs a document_id"):
            await (await executor(harness)).run(call(READ_DOCUMENT.name, document_id=""))


class TestUnknownTools:
    async def test_it_names_the_tools_that_do_exist(self) -> None:
        harness = Harness()
        with pytest.raises(UnknownToolError, match="search_corpus, read_document"):
            await (await executor(harness)).run(call("delete_everything"))
