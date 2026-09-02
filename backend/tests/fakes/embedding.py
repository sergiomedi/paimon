"""A deterministic in-memory EmbeddingModel."""

import hashlib
import math
import re
from collections.abc import Sequence

from paimon.domain.value_objects import Embedding

_TOKEN = re.compile(r"[a-z0-9]+")


class FakeEmbeddingModel:
    """Feature-hashed bag of words, L2-normalized.

    Deterministic and dependency-free, and — unlike random vectors — it preserves
    the one quality invariant the contract asserts: texts that share words land
    closer together than texts that share none. That keeps the contract suite
    meaningful without pretending a fake has a real model's semantics.
    """

    def __init__(self, dimensions: int = 64, model_id: str = "fake-embed-v1") -> None:
        self._dimensions = dimensions
        self._model_id = model_id
        # Recorded so tests can assert that work was skipped, not merely that a
        # result said it was.
        self.document_batches: list[list[str]] = []
        self.query_calls: list[str] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def _vector(self, text: str) -> Embedding:
        buckets = [0.0] * self._dimensions
        for token in _TOKEN.findall(text.lower()):
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            buckets[int.from_bytes(digest, "big") % self._dimensions] += 1.0
        norm = math.sqrt(sum(value * value for value in buckets))
        if norm:
            buckets = [value / norm for value in buckets]
        else:
            buckets[0] = 1.0
        return Embedding(values=tuple(buckets), model_id=self._model_id)

    async def embed_documents(self, texts: Sequence[str]) -> list[Embedding]:
        self.document_batches.append(list(texts))
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> Embedding:
        self.query_calls.append(text)
        return self._vector(text)
