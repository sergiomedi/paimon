"""Tests for prompt assembly."""

from paimon.domain.entities import Chunk
from paimon.infrastructure.tokenization import HeuristicTokenCounter
from paimon.rag.prompting import build_prompt


def chunk(chunk_id: str, text: str, *, headings: tuple[str, ...] = ()) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        document_id="runbook",
        tenant_id="tenant-1",
        ordinal=0,
        text=text,
        start_char=0,
        end_char=len(text),
        token_count=max(len(text.split()), 1),
        heading_path=headings,
    )


class TestStructure:
    def test_the_system_message_forbids_answering_from_memory(self) -> None:
        """The instruction that separates this from a chat wrapper."""
        prompt = build_prompt("q", [chunk("c1", "body")], HeuristicTokenCounter())
        system = prompt.messages[0]

        assert system.role == "system"
        assert "only from the numbered sources" in system.content

    def test_sources_are_numbered_from_one(self) -> None:
        prompt = build_prompt(
            "q",
            [chunk("c1", "first"), chunk("c2", "second")],
            HeuristicTokenCounter(),
        )
        user = prompt.messages[1].content

        assert "[1] runbook" in user
        assert "[2] runbook" in user

    def test_the_question_is_included(self) -> None:
        prompt = build_prompt(
            "why did eviction hang", [chunk("c1", "body")], HeuristicTokenCounter()
        )
        assert "why did eviction hang" in prompt.messages[1].content

    def test_headings_give_a_fragment_its_context(self) -> None:
        prompt = build_prompt(
            "q",
            [chunk("c1", "Run this first.", headings=("Upgrades", "Pre-flight"))],
            HeuristicTokenCounter(),
        )
        assert "Upgrades > Pre-flight" in prompt.messages[1].content

    def test_the_sources_travel_with_the_prompt(self) -> None:
        """Numbering is positional, so messages without their sources leave the
        answer's markers unresolvable."""
        chunks = [chunk("c1", "first"), chunk("c2", "second")]
        prompt = build_prompt("q", chunks, HeuristicTokenCounter())

        assert [source.chunk_id for source in prompt.sources] == ["c1", "c2"]


class TestBudget:
    def test_chunks_beyond_the_budget_are_left_out(self) -> None:
        chunks = [chunk(f"c{index}", "word " * 50) for index in range(10)]
        prompt = build_prompt("q", chunks, HeuristicTokenCounter(), max_context_tokens=200)

        assert 0 < len(prompt.sources) < len(chunks)

    def test_the_best_ranked_material_survives(self) -> None:
        """Chunks arrive best first, so truncation must drop from the end."""
        chunks = [chunk(f"c{index}", "word " * 50) for index in range(10)]
        prompt = build_prompt("q", chunks, HeuristicTokenCounter(), max_context_tokens=200)

        assert prompt.sources[0].chunk_id == "c0"

    def test_one_oversized_chunk_is_still_included_whole(self) -> None:
        """Truncating the middle of a chunk yields a procedure with steps missing
        and no way for the model to know it."""
        big = chunk("c1", "word " * 500)
        prompt = build_prompt("q", [big], HeuristicTokenCounter(), max_context_tokens=50)

        assert len(prompt.sources) == 1
        assert big.text in prompt.messages[1].content

    def test_no_chunks_says_so_rather_than_pretending(self) -> None:
        prompt = build_prompt("q", [], HeuristicTokenCounter())

        assert prompt.sources == ()
        assert "no sources were retrieved" in prompt.messages[1].content
