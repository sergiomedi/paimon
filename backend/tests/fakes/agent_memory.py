"""An in-memory AgentMemory.

The reference implementation the contract is written against, and a supported
deployment when nothing needs to survive the process — an evaluation run, a
single-process demo. Recall ranks by cosine similarity over the same embedding
port the PostgreSQL adapter uses, so the two differ in where the vectors live
and in nothing else.
"""

import math
from collections.abc import Mapping, Sequence

from paimon.domain.errors import AgentMemoryError
from paimon.domain.ports import EmbeddingModel

SUMMARY_FIELD = "summary"


def _summarise(value: Mapping[str, str]) -> str:
    if summary := value.get(SUMMARY_FIELD, "").strip():
        return summary
    return " ".join(str(item) for item in value.values() if str(item).strip())


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    magnitude = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return 0.0 if magnitude == 0 else dot / magnitude


class InMemoryAgentMemory:
    """Keeps memories in a dictionary, keyed by namespace and key."""

    def __init__(self, embedding_model: EmbeddingModel) -> None:
        """Initialise the store with the model used to embed and to recall."""
        self._embedding_model = embedding_model
        self._entries: dict[tuple[tuple[str, ...], str], tuple[dict[str, str], list[float]]] = {}

    async def remember(self, namespace: Sequence[str], key: str, value: Mapping[str, str]) -> None:
        """Write a memory, replacing any earlier one under the same key."""
        summary = _summarise(value)
        if not summary:
            msg = "a memory with no text cannot be recalled, so it is not stored"
            raise AgentMemoryError(msg)
        embeddings = await self._embedding_model.embed_documents([summary])
        self._entries[(tuple(namespace), key)] = (
            {str(name): str(item) for name, item in value.items()},
            list(embeddings[0].values),
        )

    async def recall(
        self, namespace: Sequence[str], query: str, *, limit: int = 5
    ) -> Sequence[Mapping[str, str]]:
        """Return the memories in a namespace most relevant to a query."""
        if not query.strip():
            return []
        wanted = tuple(namespace)
        candidates = [
            (content, vector)
            for (stored, _), (content, vector) in self._entries.items()
            if stored == wanted
        ]
        if not candidates:
            return []
        embedding = await self._embedding_model.embed_query(query)
        ranked = sorted(
            candidates,
            key=lambda entry: _cosine(entry[1], list(embedding.values)),
            reverse=True,
        )
        return [content for content, _ in ranked[:limit]]
