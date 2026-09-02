"""Tests for the heuristic token counter."""

import pytest

from paimon.infrastructure.tokenization import HeuristicTokenCounter


@pytest.fixture
def counter() -> HeuristicTokenCounter:
    return HeuristicTokenCounter()


class TestCounting:
    @pytest.mark.parametrize("text", ["", "   ", "\n\t "])
    def test_empty_text_counts_zero(self, counter: HeuristicTokenCounter, text: str) -> None:
        assert counter.count(text) == 0

    def test_non_empty_text_counts_at_least_one(self, counter: HeuristicTokenCounter) -> None:
        assert counter.count("a") >= 1

    def test_it_grows_with_length(self, counter: HeuristicTokenCounter) -> None:
        assert counter.count("one two three four") > counter.count("one two")

    def test_it_is_deterministic(self, counter: HeuristicTokenCounter) -> None:
        assert counter.count("cordon the node") == counter.count("cordon the node")

    def test_punctuation_counts(self, counter: HeuristicTokenCounter) -> None:
        """Sub-word tokenizers emit punctuation as its own token, so ignoring it
        would understate a prompt's real size."""
        assert counter.count("a, b, c") > counter.count("a b c")

    def test_it_over_estimates_rather_than_under(self, counter: HeuristicTokenCounter) -> None:
        """Overshooting produces chunks slightly too small; undershooting produces
        prompts the provider rejects."""
        text = "the quick brown fox jumps over the lazy dog"
        assert counter.count(text) >= len(text.split())
