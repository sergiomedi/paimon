"""Tests for structure-aware chunking."""

from itertools import pairwise

import pytest

from paimon.domain.entities import Document
from paimon.infrastructure.tokenization import HeuristicTokenCounter
from paimon.rag.chunking import Chunker, ChunkingPolicy, chunk_document

RUNBOOK = """# Node maintenance

Nodes are drained before any kernel upgrade.

## Draining

Cordon the node first so the scheduler stops placing new pods on it.
Then evict the running pods and wait for them to reschedule elsewhere.

### Rollback

Uncordon the node to return it to service.

## Known issues

Eviction stalls when a pod has no disruption budget.
"""


@pytest.fixture
def counter() -> HeuristicTokenCounter:
    return HeuristicTokenCounter()


def document(text: str = RUNBOOK) -> Document:
    return Document(
        document_id="doc-1",
        tenant_id="tenant-1",
        source_uri="https://example.test/runbook.md",
        title="Node maintenance",
        text=text,
        content_hash="hash",
        media_type="text/markdown",
    )


class TestInvariants:
    """The two properties everything downstream relies on."""

    def test_every_chunk_is_a_verbatim_slice_of_its_document(
        self, counter: HeuristicTokenCounter
    ) -> None:
        """A reworded or re-joined chunk cannot be cited: its offsets stop
        pointing at what it says."""
        doc = document()
        for chunk in chunk_document(doc, ChunkingPolicy(max_tokens=40, overlap_tokens=8), counter):
            assert doc.text[chunk.start_char : chunk.end_char] == chunk.text

    def test_no_chunk_spans_two_heading_paths(self, counter: HeuristicTokenCounter) -> None:
        """A chunk straddling two sections hands the model two contexts and lets
        it attribute a claim to the wrong one."""
        doc = document()
        for chunk in chunk_document(doc, ChunkingPolicy(max_tokens=1000), counter):
            span = doc.text[chunk.start_char : chunk.end_char]
            headings = [line for line in span.splitlines() if line.startswith("#")]
            assert len(headings) <= 1

    def test_ordinals_run_from_zero_without_gaps(self, counter: HeuristicTokenCounter) -> None:
        chunks = chunk_document(
            document(), ChunkingPolicy(max_tokens=40, overlap_tokens=8), counter
        )
        assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))

    def test_chunk_ids_are_derived_from_the_document(self, counter: HeuristicTokenCounter) -> None:
        """Deterministic ids are what make re-ingestion an upsert rather than a
        second copy."""
        chunks = chunk_document(
            document(), ChunkingPolicy(max_tokens=40, overlap_tokens=8), counter
        )
        assert [chunk.chunk_id for chunk in chunks] == [
            f"doc-1:{index}" for index in range(len(chunks))
        ]

    def test_chunking_is_deterministic(self, counter: HeuristicTokenCounter) -> None:
        policy = ChunkingPolicy(max_tokens=40, overlap_tokens=8)
        first = chunk_document(document(), policy, counter)
        second = chunk_document(document(), policy, counter)
        assert first == second


class TestHeadingStructure:
    def test_heading_paths_nest(self, counter: HeuristicTokenCounter) -> None:
        chunks = chunk_document(document(), ChunkingPolicy(max_tokens=1000), counter)
        paths = {chunk.heading_path for chunk in chunks}

        assert ("Node maintenance", "Draining") in paths
        assert ("Node maintenance", "Draining", "Rollback") in paths

    def test_a_shallower_heading_pops_the_deeper_ones(self, counter: HeuristicTokenCounter) -> None:
        """ "Known issues" is a level two heading after a level three, so it must
        replace "Rollback" rather than nest under it."""
        chunks = chunk_document(document(), ChunkingPolicy(max_tokens=1000), counter)
        known = next(chunk for chunk in chunks if "Eviction stalls" in chunk.text)

        assert known.heading_path == ("Node maintenance", "Known issues")

    def test_the_embedded_text_carries_the_heading_context(
        self, counter: HeuristicTokenCounter
    ) -> None:
        """A fragment like "Uncordon the node" says nothing on its own about what
        procedure it belongs to."""
        chunks = chunk_document(document(), ChunkingPolicy(max_tokens=1000), counter)
        rollback = next(chunk for chunk in chunks if "Uncordon" in chunk.text)

        assert rollback.embedding_text.startswith("Node maintenance > Draining > Rollback")
        assert rollback.text in rollback.embedding_text


class TestBudget:
    def test_chunks_stay_within_the_budget(self, counter: HeuristicTokenCounter) -> None:
        policy = ChunkingPolicy(max_tokens=30, overlap_tokens=0, min_tokens=1)
        for chunk in chunk_document(document(), policy, counter):
            assert chunk.token_count <= policy.max_tokens

    def test_a_paragraph_larger_than_the_budget_is_split(
        self, counter: HeuristicTokenCounter
    ) -> None:
        """A single oversized paragraph must not become a single oversized chunk."""
        paragraph = " ".join(f"Sentence number {index} explains a step." for index in range(40))
        chunks = chunk_document(
            document(f"# Long\n\n{paragraph}\n"),
            ChunkingPolicy(max_tokens=40, overlap_tokens=0, min_tokens=1),
            counter,
        )

        assert len(chunks) > 1
        assert all(chunk.token_count <= 40 for chunk in chunks)

    def test_the_split_is_still_verbatim(self, counter: HeuristicTokenCounter) -> None:
        paragraph = " ".join(f"Sentence number {index} explains a step." for index in range(40))
        doc = document(f"# Long\n\n{paragraph}\n")
        for chunk in chunk_document(
            doc, ChunkingPolicy(max_tokens=40, overlap_tokens=0, min_tokens=1), counter
        ):
            assert doc.text[chunk.start_char : chunk.end_char] == chunk.text


class TestOverlap:
    def test_overlap_repeats_the_end_of_the_previous_chunk(
        self, counter: HeuristicTokenCounter
    ) -> None:
        """A passage split across a boundary must remain retrievable from either
        side of it."""
        body = "\n\n".join(f"Paragraph {index} describes a step." for index in range(12))
        doc = document(f"# Steps\n\n{body}\n")

        without = chunk_document(
            doc, ChunkingPolicy(max_tokens=30, overlap_tokens=0, min_tokens=1), counter
        )
        with_overlap = chunk_document(
            doc, ChunkingPolicy(max_tokens=30, overlap_tokens=12, min_tokens=1), counter
        )

        assert len(with_overlap) >= len(without)
        assert any(later.start_char < earlier.end_char for earlier, later in pairwise(with_overlap))

    def test_overlap_terminates(self, counter: HeuristicTokenCounter) -> None:
        """An overlap that consumed the whole previous chunk would let a chunk
        make no forward progress."""
        body = "\n\n".join(f"Paragraph {index}." for index in range(30))
        chunks = chunk_document(
            document(f"# Steps\n\n{body}\n"),
            ChunkingPolicy(max_tokens=20, overlap_tokens=19, min_tokens=1),
            counter,
        )

        assert chunks
        assert all(later.start_char > earlier.start_char for earlier, later in pairwise(chunks))


class TestMinimumSize:
    def test_a_heading_with_no_body_is_dropped(self, counter: HeuristicTokenCounter) -> None:
        """It matches on its title words and then carries no answer."""
        doc = document("# Empty section\n\n## Also empty\n")
        assert chunk_document(doc, ChunkingPolicy(max_tokens=512, min_tokens=16), counter) == []

    def test_a_document_of_whitespace_produces_nothing(
        self, counter: HeuristicTokenCounter
    ) -> None:
        assert chunk_document(document("   \n\n  \n"), ChunkingPolicy(), counter) == []


class TestPolicy:
    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"max_tokens": 0}, "max_tokens must be positive"),
            ({"overlap_tokens": -1}, "cannot be negative"),
            ({"max_tokens": 10, "overlap_tokens": 10}, "leave room for new content"),
            (
                {"max_tokens": 10, "overlap_tokens": 0, "min_tokens": 20},
                "cannot exceed max_tokens",
            ),
        ],
    )
    def test_an_unworkable_policy_is_refused(self, kwargs: dict[str, int], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            ChunkingPolicy(**kwargs)


class TestChunker:
    def test_it_applies_its_policy(self, counter: HeuristicTokenCounter) -> None:
        policy = ChunkingPolicy(max_tokens=40, overlap_tokens=8)
        chunker = Chunker(policy, counter)

        assert chunker.policy is policy
        assert chunker.split(document()) == chunk_document(document(), policy, counter)
