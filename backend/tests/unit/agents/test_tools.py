"""The tools the platform offers a model, and what running them does."""

import pytest
from tests.unit.agents.conftest import RUNBOOK, TENANT, Harness, chunk

from paimon.agents.tools import (
    MAX_DOCUMENT_CHARACTERS,
    READ_DOCUMENT,
    SEARCH_CORPUS,
    TOOLS,
    TRUNCATION_NOTE,
    ToolArgumentError,
    ToolExecutor,
    UnknownToolError,
    render_passages,
)
from paimon.domain.entities import Document
from paimon.domain.ports import ToolCall, ToolDefinition


def call(name: str, **arguments: object) -> ToolCall:
    return ToolCall(call_id="c-1", name=name, arguments=arguments)


async def executor(harness: Harness) -> ToolExecutor:
    return harness.corpus().for_tenant(TENANT)


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
        other = harness.corpus().for_tenant("tenant-b")
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


class TestExecutingForAnAgent:
    """The path a loop uses: passages, unnumbered, with their offsets.

    ``run`` renders for an MCP client whose every call stands alone; ``execute``
    hands back what was found and lets the caller decide the numbering. The
    difference exists because an agent's final answer cites across a whole run.
    """

    async def test_searching_returns_citable_passages(self) -> None:
        harness = Harness()
        await harness.index()

        found = await (await executor(harness)).execute(call(SEARCH_CORPUS.name, query="draining"))

        assert found.passages
        assert all(passage.document_id for passage in found.passages)
        assert all(passage.end_char > passage.start_char for passage in found.passages)

    async def test_an_empty_search_carries_the_warning_as_a_note(self) -> None:
        harness = Harness()

        found = await (await executor(harness)).execute(call(SEARCH_CORPUS.name, query="draining"))

        assert found.passages == ()
        assert "Do not answer from memory" in found.note

    async def test_reading_returns_the_documents_indexed_chunks(self) -> None:
        # Not one synthetic chunk spanning the whole document. A claim drawn
        # from the third paragraph has to cite the third paragraph, and a
        # whole-document citation points a reader at everything and so at
        # nothing.
        harness = Harness()
        await harness.index_chunks(
            chunk("h-2", "handbook", "Escalation goes to the secondary.", ordinal=1),
            chunk("h-0", "handbook", "Acknowledge within five minutes.", ordinal=0),
        )

        found = await (await executor(harness)).execute(
            call(READ_DOCUMENT.name, document_id="handbook")
        )

        assert [passage.chunk_id for passage in found.passages] == ["h-0", "h-2"]

    async def test_a_read_passage_keeps_the_offsets_it_was_chunked_at(self) -> None:
        harness = Harness()
        await harness.index_chunks(
            chunk("h-1", "handbook", "Escalation goes to the secondary.", ordinal=1)
        )

        found = await (await executor(harness)).execute(
            call(READ_DOCUMENT.name, document_id="handbook")
        )

        assert found.passages[0].start_char == 1000
        assert found.passages[0].end_char == 1033

    async def test_reading_an_unindexed_document_says_so(self) -> None:
        harness = Harness()

        found = await (await executor(harness)).execute(
            call(READ_DOCUMENT.name, document_id="nothing")
        )

        assert found.passages == ()
        assert "No document 'nothing'" in found.note

    async def test_a_long_document_is_cut_and_the_cut_is_announced(self) -> None:
        harness = Harness()
        paragraph = "step. " * 400
        await harness.index_chunks(
            *(
                chunk(f"big-{index}", "big", paragraph, ordinal=index, start_char=index * 2400)
                for index in range(5)
            )
        )

        found = await (await executor(harness)).execute(call(READ_DOCUMENT.name, document_id="big"))

        assert found.truncated
        assert len(found.passages) < 5
        assert found.note == TRUNCATION_NOTE

    async def test_a_document_that_fits_is_not_announced_as_cut(self) -> None:
        harness = Harness()
        await harness.index_chunks(chunk("s-0", "small", "Acknowledge within five minutes."))

        found = await (await executor(harness)).execute(
            call(READ_DOCUMENT.name, document_id="small")
        )

        assert not found.truncated
        assert found.note == ""

    async def test_a_model_cannot_read_another_tenants_document(self) -> None:
        harness = Harness()
        await harness.index()
        other = harness.corpus().for_tenant("tenant-b")

        found = await other.execute(call(READ_DOCUMENT.name, document_id="runbook"))

        assert found.passages == ()
        assert "No document 'runbook'" in found.note

    async def test_an_unknown_tool_still_names_what_exists(self) -> None:
        harness = Harness()
        with pytest.raises(UnknownToolError, match="search_corpus, read_document"):
            await (await executor(harness)).execute(call("delete_everything"))


class TestNumbering:
    """Who decides what a passage is called.

    ``render_passages`` takes the markers rather than deriving them, because the
    whole difference between a single tool call and a running conversation is
    that in the second one the numbering has to mean the same thing on turn
    three as it did on turn one.
    """

    def test_it_renders_the_markers_it_is_given(self) -> None:
        passages = (
            chunk("a", "runbook", "Cordon first."),
            chunk("b", "handbook", "Acknowledge within five minutes."),
        )

        rendered = render_passages(passages, [7, 8])

        assert rendered == (
            "[7] document: runbook\nCordon first.\n\n"
            "[8] document: handbook\nAcknowledge within five minutes."
        )

    def test_a_marker_per_passage_is_required(self) -> None:
        # A passage rendered under the wrong number is a citation that resolves
        # to the wrong text, which is worse than one that does not resolve.
        with pytest.raises(ValueError, match="2 passages and 1 markers"):
            render_passages(
                (chunk("a", "runbook", "one"), chunk("b", "runbook", "two")),
                [1],
            )

    def test_nothing_renders_as_nothing(self) -> None:
        assert render_passages((), []) == ""


class TestWhatAnMcpClientStillSees:
    """The rendering an external client gets, pinned byte for byte.

    ``execute`` was added beside ``run``, not in place of it. These assert the
    exact strings rather than a substring, because "MCP is unchanged" is a claim
    about the bytes and a substring check would pass through a reformatting that
    broke every client parsing it.
    """

    async def test_a_search_is_numbered_from_one(self) -> None:
        harness = Harness()
        await harness.index_chunks(
            chunk("a", "runbook", "Cordon first."),
            chunk("b", "handbook", "Acknowledge within five minutes."),
        )

        rendered = await (await executor(harness)).run(call(SEARCH_CORPUS.name, query="cordon"))

        assert rendered.startswith("[1] document: ")
        assert "\n\n[2] document: " in rendered

    async def test_an_empty_search_is_the_same_sentence_as_before(self) -> None:
        harness = Harness()

        rendered = await (await executor(harness)).run(call(SEARCH_CORPUS.name, query="cordon"))

        assert rendered == "No indexed passages matched that query. Do not answer from memory."

    async def test_a_read_is_still_the_document_not_its_chunks(self) -> None:
        # Chunking overlaps, so a document reassembled from its passages would
        # repeat itself. A client asking for a document is asking for the
        # document, and this surface keeps giving it one.
        harness = Harness()
        await harness.index()

        rendered = await (await executor(harness)).run(
            call(READ_DOCUMENT.name, document_id="runbook")
        )

        assert rendered == RUNBOOK
        assert "[1] document:" not in rendered

    async def test_an_unknown_document_is_the_same_sentence_as_before(self) -> None:
        harness = Harness()

        rendered = await (await executor(harness)).run(
            call(READ_DOCUMENT.name, document_id="nothing")
        )

        assert rendered == "No document 'nothing' is indexed for this tenant."
