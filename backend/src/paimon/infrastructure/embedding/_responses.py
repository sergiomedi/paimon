"""Parsing embedding responses.

Shared by the OpenAI-compatible and Azure OpenAI adapters, which differ in how a
request is addressed and authenticated but not at all in what comes back. The
fiddly, safety-critical part is the parsing, so it lives in one place rather than
in two that can drift.
"""

from typing import Any

from paimon.domain.errors import EmbeddingError
from paimon.domain.value_objects import Embedding


def parse_embeddings(body: Any, expected: int, dimensions: int, model_id: str) -> list[Embedding]:
    """Turn an embeddings response into domain embeddings.

    Args:
        body: The decoded response body.
        expected: How many embeddings were asked for.
        dimensions: The width the index is built on.
        model_id: Identifier to stamp on each embedding.

    Returns:
        Embeddings in input order.

    Raises:
        EmbeddingError: If the body is malformed, incomplete, or the wrong width.
    """
    try:
        data = list(body["data"])
    except (KeyError, TypeError) as error:
        msg = "embedding response has no 'data' array"
        raise EmbeddingError(msg) from error

    if len(data) != expected:
        msg = f"asked for {expected} embeddings, received {len(data)}"
        raise EmbeddingError(msg)

    # Ordered by the index the provider reports rather than by arrival. Results
    # are matched to inputs by position, so a provider that answers out of order
    # would attach every embedding to the wrong chunk, silently.
    try:
        data.sort(key=lambda item: int(item["index"]))
        vectors = [tuple(float(value) for value in item["embedding"]) for item in data]
    except (KeyError, TypeError, ValueError) as error:
        msg = f"embedding response is malformed: {error}"
        raise EmbeddingError(msg) from error

    for vector in vectors:
        if len(vector) != dimensions:
            msg = (
                f"model '{model_id}' returned {len(vector)} dimensions, "
                f"but the index is built on {dimensions}"
            )
            raise EmbeddingError(msg)

    return [Embedding(values=vector, model_id=model_id) for vector in vectors]
