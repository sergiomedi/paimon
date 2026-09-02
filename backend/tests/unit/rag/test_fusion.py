"""Tests for reciprocal rank fusion."""

import pytest

from paimon.domain.entities import Chunk
from paimon.domain.ports import SearchHit
from paimon.rag.fusion import DEFAULT_RRF_K, reciprocal_rank_fusion


def chunk(chunk_id: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        document_id="doc-1",
        tenant_id="tenant-1",
        ordinal=0,
        text=f"text of {chunk_id}",
        start_char=0,
        end_char=20,
        token_count=4,
    )


def ranked(retriever: str, *chunk_ids: str, scores: list[float] | None = None) -> list[SearchHit]:
    """A retriever's result list, ranked from one."""
    return [
        SearchHit(
            chunk=chunk(chunk_id),
            score=scores[index] if scores else 1.0 / (index + 1),
            rank=index + 1,
            retriever=retriever,
        )
        for index, chunk_id in enumerate(chunk_ids)
    ]


class TestOrdering:
    def test_a_chunk_both_retrievers_found_outranks_one_only_either_found(self) -> None:
        """Agreement between retrievers is the signal fusion exists to reward."""
        fused = reciprocal_rank_fusion(
            [ranked("dense", "a", "b"), ranked("lexical", "c", "a")],
            top_k=3,
        )
        assert fused[0].chunk.chunk_id == "a"

    def test_ranks_are_one_based_and_contiguous(self) -> None:
        fused = reciprocal_rank_fusion(
            [ranked("dense", "a", "b", "c"), ranked("lexical", "c", "d")], top_k=10
        )
        assert [hit.rank for hit in fused] == list(range(1, len(fused) + 1))

    def test_scores_descend(self) -> None:
        fused = reciprocal_rank_fusion(
            [ranked("dense", "a", "b", "c"), ranked("lexical", "b", "c")], top_k=10
        )
        assert [hit.score for hit in fused] == sorted((h.score for h in fused), reverse=True)

    def test_top_k_truncates(self) -> None:
        fused = reciprocal_rank_fusion([ranked("dense", "a", "b", "c", "d")], top_k=2)
        assert len(fused) == 2

    def test_it_is_deterministic_when_scores_tie(self) -> None:
        """An evaluation run that reorders equal-scoring hits between runs reports
        noise as a change."""
        lists = [ranked("dense", "b", "a"), ranked("lexical", "a", "b")]
        first = reciprocal_rank_fusion(lists, top_k=5)
        second = reciprocal_rank_fusion(lists, top_k=5)

        assert [hit.chunk.chunk_id for hit in first] == [hit.chunk.chunk_id for hit in second]


class TestScaleIndependence:
    def test_an_unbounded_score_does_not_dominate(self) -> None:
        """BM25 is unbounded and cosine similarity is not. Fusing the numbers
        would let the lexical retriever decide every ordering by itself; fusing
        the positions cannot."""
        lexical = ranked("lexical", "x", "y", scores=[9_999.0, 9_000.0])
        dense = ranked("dense", "y", "x", scores=[0.81, 0.79])

        fused = reciprocal_rank_fusion([dense, lexical], top_k=2)
        assert {hit.chunk.chunk_id for hit in fused} == {"x", "y"}
        assert fused[0].score == pytest.approx(fused[1].score)


class TestRecall:
    def test_a_chunk_only_one_retriever_found_still_appears(self) -> None:
        """The case that justifies hybrid retrieval: an exact token one retriever
        cannot embed, or a paraphrase the other cannot match."""
        fused = reciprocal_rank_fusion(
            [ranked("dense", "paraphrase-match"), ranked("lexical", "exact-token-match")],
            top_k=10,
        )
        assert {hit.chunk.chunk_id for hit in fused} == {
            "paraphrase-match",
            "exact-token-match",
        }

    def test_an_empty_retriever_does_not_break_fusion(self) -> None:
        fused = reciprocal_rank_fusion([ranked("dense", "a", "b"), []], top_k=5)
        assert [hit.chunk.chunk_id for hit in fused] == ["a", "b"]

    def test_no_results_fuse_to_nothing(self) -> None:
        assert reciprocal_rank_fusion([[], []], top_k=5) == []


class TestProvenance:
    def test_each_retriever_that_found_a_chunk_is_recorded(self) -> None:
        """ "Only lexical found this, at rank seven" is the most useful thing to
        know about a surprising result, and it cannot be reconstructed later."""
        fused = reciprocal_rank_fusion([ranked("dense", "a"), ranked("lexical", "b", "a")], top_k=5)
        found = next(hit for hit in fused if hit.chunk.chunk_id == "a")

        assert found.retrievers == ("dense", "lexical")
        assert {(c.retriever, c.rank) for c in found.contributions} == {
            ("dense", 1),
            ("lexical", 2),
        }

    def test_the_original_scores_are_preserved(self) -> None:
        fused = reciprocal_rank_fusion([ranked("dense", "a", scores=[0.42])], top_k=1)
        assert fused[0].contributions[0].score == pytest.approx(0.42)


class TestWeighting:
    def test_a_weighted_retriever_wins_a_tie(self) -> None:
        lists = [ranked("dense", "a"), ranked("lexical", "b")]

        assert reciprocal_rank_fusion(lists, top_k=1)[0].chunk.chunk_id == "a"
        weighted = reciprocal_rank_fusion(lists, top_k=1, weights={"lexical": 2.0})
        assert weighted[0].chunk.chunk_id == "b"

    def test_a_missing_weight_defaults_to_one(self) -> None:
        lists = [ranked("dense", "a"), ranked("lexical", "b")]
        assert reciprocal_rank_fusion(lists, top_k=2, weights={"dense": 1.0}) == (
            reciprocal_rank_fusion(lists, top_k=2)
        )


class TestSmoothing:
    def test_the_default_matches_the_constant_azure_uses(self) -> None:
        """Both backends must order comparably, or the Phase 6 comparison measures
        two fusion algorithms rather than two retrieval backends."""
        assert DEFAULT_RRF_K == 60

    def test_a_smaller_constant_weights_the_top_positions_more(self) -> None:
        lists = [ranked("dense", "first", "second")]

        gentle = reciprocal_rank_fusion(lists, top_k=2, rrf_k=60)
        sharp = reciprocal_rank_fusion(lists, top_k=2, rrf_k=0)

        assert gentle[0].score / gentle[1].score < sharp[0].score / sharp[1].score

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [({"rrf_k": -1}, "cannot be negative"), ({"top_k": 0}, "must be positive")],
    )
    def test_unworkable_parameters_are_refused(self, kwargs: dict[str, int], message: str) -> None:
        call: dict[str, int] = {"top_k": 5}
        call.update(kwargs)
        with pytest.raises(ValueError, match=message):
            reciprocal_rank_fusion([ranked("dense", "a")], **call)  # type: ignore[arg-type]
