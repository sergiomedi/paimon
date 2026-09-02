"""Embeddings as a domain value object."""

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Embedding:
    """A dense vector together with the model that produced it.

    The model id travels with the vector deliberately. Mixing embeddings from two
    models in one index produces a system that retrieves nonsense without ever
    failing, which is among the most expensive bugs to diagnose because nothing
    reports an error. Carrying the provenance makes the mismatch detectable at the
    point of writing rather than months later in bad answers.

    Attributes:
        values: The vector itself.
        model_id: Identifier of the model that produced it.
    """

    values: tuple[float, ...]
    model_id: str

    def __post_init__(self) -> None:
        """Reject a vector that cannot be indexed or compared.

        Raises:
            ValueError: If the vector is empty or the model id is blank.
        """
        if not self.values:
            msg = "an embedding must have at least one dimension"
            raise ValueError(msg)
        if not self.model_id.strip():
            msg = "an embedding must record the model that produced it"
            raise ValueError(msg)

    @property
    def dimensions(self) -> int:
        """Number of dimensions in the vector."""
        return len(self.values)


def cosine_similarity(left: Embedding, right: Embedding) -> float:
    """Cosine similarity between two embeddings.

    Args:
        left: First embedding.
        right: Second embedding.

    Returns:
        A value in [-1, 1]; 1 means identical direction.

    Raises:
        ValueError: If the embeddings come from different models or differ in
            dimensionality. Comparing across models is meaningless, so it is
            refused rather than silently producing a number.
    """
    if left.model_id != right.model_id:
        msg = f"cannot compare embeddings from '{left.model_id}' and '{right.model_id}'"
        raise ValueError(msg)
    if left.dimensions != right.dimensions:
        msg = f"dimension mismatch: {left.dimensions} vs {right.dimensions}"
        raise ValueError(msg)

    dot = sum(a * b for a, b in zip(left.values, right.values, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left.values))
    right_norm = math.sqrt(sum(b * b for b in right.values))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)
