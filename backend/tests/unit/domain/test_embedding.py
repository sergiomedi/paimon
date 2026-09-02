"""Tests for the Embedding value object."""

import pytest

from paimon.domain.value_objects import Embedding, cosine_similarity


def embedding(*values: float, model_id: str = "model-a") -> Embedding:
    return Embedding(values=values, model_id=model_id)


class TestConstruction:
    def test_it_reports_its_dimensions(self) -> None:
        assert embedding(0.1, 0.2, 0.3).dimensions == 3

    def test_an_empty_vector_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one dimension"):
            Embedding(values=(), model_id="model-a")

    @pytest.mark.parametrize("model_id", ["", "   "])
    def test_an_embedding_without_a_model_is_refused(self, model_id: str) -> None:
        """Provenance is what makes an index mismatch detectable at all."""
        with pytest.raises(ValueError, match="record the model"):
            Embedding(values=(0.1,), model_id=model_id)


class TestCosineSimilarity:
    def test_an_identical_vector_scores_one(self) -> None:
        subject = embedding(0.0, 1.0, 0.0)
        assert cosine_similarity(subject, subject) == pytest.approx(1.0)

    def test_an_orthogonal_vector_scores_zero(self) -> None:
        assert cosine_similarity(embedding(1.0, 0.0), embedding(0.0, 1.0)) == pytest.approx(0.0)

    def test_an_opposite_vector_scores_minus_one(self) -> None:
        assert cosine_similarity(embedding(1.0, 0.0), embedding(-1.0, 0.0)) == pytest.approx(-1.0)

    def test_magnitude_does_not_matter(self) -> None:
        assert cosine_similarity(embedding(1.0, 1.0), embedding(5.0, 5.0)) == pytest.approx(1.0)

    def test_a_zero_vector_scores_zero_rather_than_dividing_by_zero(self) -> None:
        assert cosine_similarity(embedding(0.0, 0.0), embedding(1.0, 1.0)) == 0.0

    def test_comparing_across_models_is_refused(self) -> None:
        """The number would be meaningless, so no number is produced."""
        with pytest.raises(ValueError, match="cannot compare embeddings"):
            cosine_similarity(embedding(1.0), embedding(1.0, model_id="model-b"))

    def test_comparing_different_sizes_is_refused(self) -> None:
        with pytest.raises(ValueError, match="dimension mismatch"):
            cosine_similarity(embedding(1.0), embedding(1.0, 2.0))
