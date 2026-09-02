"""Embeddings from any endpoint that speaks the OpenAI embeddings API."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from paimon.domain.errors import EmbeddingError
from paimon.domain.value_objects import Embedding
from paimon.infrastructure.embedding._responses import parse_embeddings

DEFAULT_BATCH_SIZE = 96
DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class OpenAICompatibleConfig:
    """Everything the adapter needs to reach one endpoint.

    Grouped rather than passed as eight arguments: they describe a single
    endpoint, and they map one-to-one onto the settings section that supplies
    them.

    Attributes:
        base_url: Root of the API, for example ``http://localhost:11434/v1``.
        model: Model name the endpoint expects.
        dimensions: Dimensionality the platform's index is built on.
        api_key: Bearer token, when the endpoint wants one.
        document_prefix: Prepended to text being indexed.
        query_prefix: Prepended to text being searched with. Several strong open
            models are asymmetric and expect an instruction here and nowhere else.
        batch_size: Texts per request; providers cap this.
        timeout_seconds: Per-request timeout.
    """

    base_url: str
    model: str
    dimensions: int
    api_key: str | None = None
    document_prefix: str = ""
    query_prefix: str = ""
    batch_size: int = DEFAULT_BATCH_SIZE
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


class OpenAICompatibleEmbeddingModel:
    """Calls an OpenAI-shaped ``/embeddings`` endpoint.

    One adapter covers every local server that implements that shape — Ollama,
    vLLM, text-embeddings-inference and the rest — so the development model can
    be swapped by changing a URL rather than by writing another adapter.

    Documents and queries are embedded through separate methods, and this class
    is where that pays off: several strong open models are asymmetric and expect
    an instruction prefix on the query side only. The prefixes are configuration,
    because they belong to the model rather than to the platform.
    """

    def __init__(
        self, config: OpenAICompatibleConfig, client: httpx.AsyncClient | None = None
    ) -> None:
        """Initialise the adapter.

        Args:
            config: The endpoint to call and how to call it.
            client: An existing client to use, mainly for tests.
        """
        self._config = config
        self._model = config.model
        self._dimensions = config.dimensions
        headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            headers=headers,
            timeout=config.timeout_seconds,
        )

    @property
    def model_id(self) -> str:
        """Identifier written onto every embedding this model produces."""
        return self._model

    @property
    def dimensions(self) -> int:
        """Dimensionality of the vectors this model produces."""
        return self._dimensions

    async def aclose(self) -> None:
        """Release the underlying connection pool."""
        await self._client.aclose()

    async def embed_documents(self, texts: Sequence[str]) -> list[Embedding]:
        """Embed text destined for the index."""
        if not texts:
            return []
        prefixed = [f"{self._config.document_prefix}{text}" for text in texts]
        embeddings: list[Embedding] = []
        for start in range(0, len(prefixed), self._config.batch_size):
            embeddings.extend(
                await self._request(prefixed[start : start + self._config.batch_size])
            )
        return embeddings

    async def embed_query(self, text: str) -> Embedding:
        """Embed a search query."""
        (embedding,) = await self._request([f"{self._config.query_prefix}{text}"])
        return embedding

    async def _request(self, inputs: list[str]) -> list[Embedding]:
        payload: dict[str, Any] = {
            "model": self._model,
            "input": inputs,
            # Honoured by providers that support Matryoshka truncation and
            # ignored by the rest, whose native size is checked below anyway.
            "dimensions": self._dimensions,
        }
        try:
            response = await self._client.post("/embeddings", json=payload)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as error:
            msg = f"embedding provider returned {error.response.status_code}"
            raise EmbeddingError(msg) from error
        except httpx.HTTPError as error:
            msg = f"embedding provider unreachable: {error}"
            raise EmbeddingError(msg) from error
        except ValueError as error:
            msg = f"embedding provider returned a malformed body: {error}"
            raise EmbeddingError(msg) from error

        return parse_embeddings(
            body, expected=len(inputs), dimensions=self._dimensions, model_id=self._model
        )
