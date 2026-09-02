"""Azure AI Search adapter for the VectorStore port.

The genuinely different backend, and therefore the real test of ADR-0003. Where
pgvector is a table this platform designed, Azure AI Search is a service with its
own schema, its own query language and its own opinions — including a native
hybrid ranker, which is why this adapter also satisfies
:class:`~paimon.domain.ports.NativeHybridSearch`.
"""

import base64
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from paimon.domain.entities import Chunk
from paimon.domain.errors import IndexMismatchError, RetrievalError
from paimon.domain.ports import ChunkRecord, IndexDescriptor, SearchFilters, SearchHit
from paimon.domain.value_objects import Embedding
from paimon.infrastructure.azure.credentials import AzureCredential

SEARCH_SCOPE = "https://search.azure.com/.default"
DEFAULT_API_VERSION = "2024-07-01"
DEFAULT_TIMEOUT_SECONDS = 30.0
# Azure caps a document-index request; batches above this are rejected outright.
MAX_BATCH = 1000
VECTOR_PROFILE = "paimon-hnsw"


@dataclass(frozen=True, slots=True)
class AzureSearchConfig:
    """Where the index lives and how to talk to it."""

    endpoint: str
    index_name: str
    embedding_model_id: str
    dimensions: int = 1024
    api_version: str = DEFAULT_API_VERSION
    semantic_configuration: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


def encode_key(chunk_id: str, tenant_id: str) -> str:
    """Encode a chunk id as an Azure Search document key.

    Azure permits only letters, digits, underscore, dash and equals in a key,
    while chunk ids contain a colon and tenant ids are arbitrary. Base64url of
    both, rather than a substitution, because a substitution that maps two
    different ids onto one key silently overwrites a chunk.
    """
    raw = f"{tenant_id}\x1f{chunk_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class AzureSearchStore:
    """Stores and retrieves chunks in Azure AI Search."""

    def __init__(
        self,
        config: AzureSearchConfig,
        credential: AzureCredential,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Initialise the adapter.

        Args:
            config: Endpoint, index and request settings.
            credential: How to authenticate; headers are fetched per request.
            client: An existing client to use, mainly for tests.
        """
        self._config = config
        self._credential = credential
        self._client = client or httpx.AsyncClient(
            base_url=config.endpoint.rstrip("/"), timeout=config.timeout_seconds
        )

    @property
    def descriptor(self) -> IndexDescriptor:
        """The index this store writes to and reads from."""
        return IndexDescriptor(
            name=self._config.index_name,
            embedding_model_id=self._config.embedding_model_id,
            dimensions=self._config.dimensions,
        )

    @property
    def supports_semantic_ranking(self) -> bool:
        """Whether a semantic configuration is available on this index.

        Exposed rather than assumed: it is a capability of this backend that
        pgvector has no equivalent for, and ADR-0003 requires such a difference to
        be visible instead of silently degraded.
        """
        return self._config.semantic_configuration is not None

    async def aclose(self) -> None:
        """Release the underlying connection pool."""
        await self._client.aclose()

    def _reject_mismatch(self, embedding: Embedding) -> None:
        if embedding.model_id != self._config.embedding_model_id:
            msg = (
                f"index '{self._config.index_name}' holds embeddings from "
                f"'{self._config.embedding_model_id}', got '{embedding.model_id}'"
            )
            raise IndexMismatchError(msg)
        if embedding.dimensions != self._config.dimensions:
            msg = (
                f"index '{self._config.index_name}' has {self._config.dimensions} "
                f"dimensions, got {embedding.dimensions}"
            )
            raise IndexMismatchError(msg)

    async def _request(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"{path}?api-version={self._config.api_version}"
        try:
            headers = await self._credential.headers()
            if payload is None:
                response = await self._client.get(url, headers=headers)
            else:
                response = await self._client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json() if response.content else {}
        except httpx.HTTPStatusError as error:
            detail = _error_detail(error.response)
            msg = f"azure ai search returned {error.response.status_code}{detail}"
            raise RetrievalError(msg) from error
        except httpx.HTTPError as error:
            msg = f"azure ai search unreachable: {error}"
            raise RetrievalError(msg) from error
        except ValueError as error:
            msg = f"azure ai search returned a malformed body: {error}"
            raise RetrievalError(msg) from error

    async def upsert(self, records: Sequence[ChunkRecord]) -> None:
        """Insert or replace chunks, keyed by tenant and chunk id."""
        for record in records:
            self._reject_mismatch(record.embedding)
        if not records:
            return

        for start in range(0, len(records), MAX_BATCH):
            batch = records[start : start + MAX_BATCH]
            payload = {
                "value": [
                    {
                        "@search.action": "mergeOrUpload",
                        "id": encode_key(record.chunk.chunk_id, record.chunk.tenant_id),
                        "chunk_id": record.chunk.chunk_id,
                        "tenant_id": record.chunk.tenant_id,
                        "document_id": record.chunk.document_id,
                        "ordinal": record.chunk.ordinal,
                        "text": record.chunk.text,
                        "heading_path": list(record.chunk.heading_path),
                        "start_char": record.chunk.start_char,
                        "end_char": record.chunk.end_char,
                        "token_count": record.chunk.token_count,
                        "embedding_model": record.embedding.model_id,
                        "embedding": list(record.embedding.values),
                    }
                    for record in batch
                ]
            }
            body = await self._request(f"/indexes/{self._config.index_name}/docs/index", payload)
            _raise_for_document_errors(body)

    async def delete_document(self, tenant_id: str, document_id: str) -> int:
        """Remove every chunk of a document.

        Two round trips: Azure deletes by key, so the keys have to be found first.
        """
        found = await self._search(
            {
                "search": "*",
                "filter": _odata_filter(SearchFilters(tenant_id=tenant_id))
                + f" and document_id eq '{_escape(document_id)}'",
                "select": "id",
                "top": MAX_BATCH,
            }
        )
        keys = [item["id"] for item in found.get("value", [])]
        if not keys:
            return 0

        payload = {"value": [{"@search.action": "delete", "id": key} for key in keys]}
        body = await self._request(f"/indexes/{self._config.index_name}/docs/index", payload)
        _raise_for_document_errors(body)
        return len(keys)

    async def _search(self, payload: dict[str, Any]) -> Any:
        return await self._request(f"/indexes/{self._config.index_name}/docs/search", payload)

    async def search_dense(
        self, embedding: Embedding, *, top_k: int, filters: SearchFilters
    ) -> list[SearchHit]:
        """Retrieve by vector similarity."""
        self._reject_mismatch(embedding)
        body = await self._search(
            {
                "count": False,
                "filter": _odata_filter(filters),
                "top": top_k,
                "vectorQueries": [
                    {
                        "kind": "vector",
                        "vector": list(embedding.values),
                        "fields": "embedding",
                        "k": top_k,
                    }
                ],
            }
        )
        return _hits(body, retriever="dense")

    async def search_lexical(
        self, query: str, *, top_k: int, filters: SearchFilters
    ) -> list[SearchHit]:
        """Retrieve by keyword matching."""
        if not query.strip():
            return []
        body = await self._search(
            {
                "search": query,
                "queryType": "simple",
                "filter": _odata_filter(filters),
                "top": top_k,
            }
        )
        return _hits(body, retriever="lexical")

    async def search_hybrid(
        self, query: str, embedding: Embedding, *, top_k: int, filters: SearchFilters
    ) -> list[SearchHit]:
        """Retrieve using the service's own fusion of both signals.

        Azure fuses with reciprocal rank at k=60, which is why the local backend
        uses the same constant (ADR-0012): the two orderings are then comparable
        rather than merely both plausible.
        """
        self._reject_mismatch(embedding)
        payload: dict[str, Any] = {
            "search": query,
            "filter": _odata_filter(filters),
            "top": top_k,
            "vectorQueries": [
                {
                    "kind": "vector",
                    "vector": list(embedding.values),
                    "fields": "embedding",
                    "k": top_k,
                }
            ],
        }
        if self._config.semantic_configuration:
            payload["queryType"] = "semantic"
            payload["semanticConfiguration"] = self._config.semantic_configuration
        body = await self._search(payload)
        return _hits(body, retriever="hybrid")

    def index_definition(self) -> dict[str, Any]:
        """The index this adapter expects, as Azure describes it.

        Returned rather than only created, so it can be reviewed, diffed and
        applied by whatever provisions infrastructure — the schema is not
        something to discover from a failed write.
        """
        definition: dict[str, Any] = {
            "name": self._config.index_name,
            "fields": [
                {"name": "id", "type": "Edm.String", "key": True, "filterable": True},
                {"name": "chunk_id", "type": "Edm.String", "filterable": True},
                {"name": "tenant_id", "type": "Edm.String", "filterable": True},
                {"name": "document_id", "type": "Edm.String", "filterable": True},
                {"name": "ordinal", "type": "Edm.Int32", "sortable": True},
                {"name": "text", "type": "Edm.String", "searchable": True},
                {
                    "name": "heading_path",
                    "type": "Collection(Edm.String)",
                    "searchable": True,
                },
                {"name": "start_char", "type": "Edm.Int32"},
                {"name": "end_char", "type": "Edm.Int32"},
                {"name": "token_count", "type": "Edm.Int32"},
                {"name": "embedding_model", "type": "Edm.String", "filterable": True},
                {
                    "name": "embedding",
                    "type": "Collection(Edm.Single)",
                    "searchable": True,
                    "dimensions": self._config.dimensions,
                    "vectorSearchProfile": VECTOR_PROFILE,
                },
            ],
            "vectorSearch": {
                "algorithms": [
                    {
                        "name": "paimon-hnsw-algorithm",
                        "kind": "hnsw",
                        # Cosine, to match the similarity the domain computes and
                        # the operator class the pgvector index uses.
                        "hnswParameters": {"metric": "cosine"},
                    }
                ],
                "profiles": [{"name": VECTOR_PROFILE, "algorithm": "paimon-hnsw-algorithm"}],
            },
        }
        if self._config.semantic_configuration:
            definition["semantic"] = {
                "configurations": [
                    {
                        "name": self._config.semantic_configuration,
                        "prioritizedFields": {
                            "prioritizedContentFields": [{"fieldName": "text"}],
                            "prioritizedKeywordsFields": [{"fieldName": "heading_path"}],
                        },
                    }
                ]
            }
        return definition

    async def ensure_index(self) -> None:
        """Create or update the index to match what this adapter expects."""
        await self._request(f"/indexes/{self._config.index_name}", self.index_definition())


def _escape(value: str) -> str:
    """Escape a value for an OData string literal."""
    return value.replace("'", "''")


def _odata_filter(filters: SearchFilters) -> str:
    """Build the OData filter, always restricting to the tenant."""
    clauses = [f"tenant_id eq '{_escape(filters.tenant_id)}'"]
    if filters.document_ids is not None:
        joined = ",".join(sorted(_escape(item) for item in filters.document_ids))
        clauses.append(f"search.in(document_id, '{joined}', ',')")
    for key, value in sorted(filters.metadata.items()):
        clauses.append(f"{key} eq '{_escape(value)}'")
    return " and ".join(clauses)


def _hits(body: Any, retriever: str) -> list[SearchHit]:
    """Turn a search response into ranked hits."""
    results = body.get("value", []) if isinstance(body, dict) else []
    hits: list[SearchHit] = []
    for position, item in enumerate(results, start=1):
        try:
            chunk = Chunk(
                chunk_id=str(item["chunk_id"]),
                document_id=str(item["document_id"]),
                tenant_id=str(item["tenant_id"]),
                ordinal=int(item["ordinal"]),
                text=str(item["text"]),
                start_char=int(item["start_char"]),
                end_char=int(item["end_char"]),
                token_count=int(item["token_count"]),
                heading_path=tuple(item.get("heading_path") or ()),
            )
        except (KeyError, TypeError, ValueError) as error:
            msg = f"azure ai search returned an unusable document: {error}"
            raise RetrievalError(msg) from error
        # Reranker score when semantic ranking ran, search score otherwise; the
        # two are on different scales, which is one more reason fusion is by rank.
        score = item.get("@search.rerankerScore", item.get("@search.score", 0.0))
        hits.append(SearchHit(chunk=chunk, score=float(score), rank=position, retriever=retriever))
    return hits


def _raise_for_document_errors(body: Any) -> None:
    """Azure reports per-document failures with a 200; find them.

    A partially applied batch that returns success is how an index quietly ends up
    missing the chunks nobody noticed were rejected.
    """
    failures = [
        item
        for item in (body.get("value", []) if isinstance(body, dict) else [])
        if not item.get("status", True)
    ]
    if failures:
        first = failures[0]
        msg = (
            f"{len(failures)} document(s) rejected by azure ai search; "
            f"first: {first.get('key')}: {first.get('errorMessage')}"
        )
        raise RetrievalError(msg)


def _error_detail(response: httpx.Response) -> str:
    """Extract Azure's own error code."""
    try:
        code = response.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return ""
    return f" ({code})"
