"""pgvector adapter for the AgentMemory port.

Recall is a similarity search, not a lookup, because the run doing the recalling
does not know the key the run that learned it used. That is the whole difference
between memory and a cache: a cache is asked for something it was told about, and
memory is asked for whatever turns out to be relevant.
"""

from collections.abc import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from paimon.domain.errors import AgentMemoryError, EmbeddingError
from paimon.domain.ports import EmbeddingModel
from paimon.infrastructure.persistence.models import AgentMemoryRow

#: The field whose text is embedded, when the caller supplies one. Memory is
#: recalled by what it is *about*, and a JSON document flattened whole embeds its
#: keys as heavily as its content.
SUMMARY_FIELD = "summary"


def _summarise(value: Mapping[str, str]) -> str:
    """Return the text a memory should be recalled by."""
    if summary := value.get(SUMMARY_FIELD, "").strip():
        return summary
    # No summary offered: fall back to the values alone. Including the keys would
    # let every memory share the vocabulary of its schema, which pulls unrelated
    # memories towards each other.
    return " ".join(str(item) for item in value.values() if str(item).strip())


class PgVectorAgentMemory:
    """Stores and recalls agent memories in PostgreSQL."""

    def __init__(
        self, engine: AsyncEngine, embedding_model: EmbeddingModel, tenant_id: str
    ) -> None:
        """Initialise the store.

        Args:
            engine: Engine whose pool connections are borrowed from.
            embedding_model: Embeds what a memory is about, and the query that
                recalls it. The same model as retrieval uses, deliberately: two
                models means two vector spaces, and a similarity between them is
                a number with no meaning.
            tenant_id: The isolation boundary. Bound at construction rather than
                passed per call, because a memory store shared across tenants by
                accident is a data leak with no error message.
        """
        self._engine = engine
        self._embedding_model = embedding_model
        self._tenant_id = tenant_id

    async def remember(self, namespace: Sequence[str], key: str, value: Mapping[str, str]) -> None:
        """Write a memory, replacing any earlier one under the same key.

        Raises:
            AgentMemoryError: If the memory could not be written.
        """
        summary = _summarise(value)
        if not summary:
            msg = "a memory with no text cannot be recalled, so it is not stored"
            raise AgentMemoryError(msg)

        try:
            embedding = await self._embedding_model.embed_documents([summary])
        except EmbeddingError as error:
            msg = f"could not embed a memory under {list(namespace)}: {error}"
            raise AgentMemoryError(msg) from error

        values = {
            "tenant_id": self._tenant_id,
            "namespace": list(namespace),
            "key": key,
            "content": dict(value),
            "summary": summary,
            "embedding": list(embedding[0].values),
            "embedding_model": embedding[0].model_id,
        }
        statement = insert(AgentMemoryRow).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[
                AgentMemoryRow.tenant_id,
                AgentMemoryRow.namespace,
                AgentMemoryRow.key,
            ],
            set_={
                key_: statement.excluded[key_]
                for key_ in ("content", "summary", "embedding", "embedding_model")
            },
        )
        try:
            async with self._engine.begin() as connection:
                await connection.execute(statement)
        except SQLAlchemyError as error:
            msg = f"could not store a memory under {list(namespace)}: {error}"
            raise AgentMemoryError(msg) from error

    async def recall(
        self, namespace: Sequence[str], query: str, *, limit: int = 5
    ) -> Sequence[Mapping[str, str]]:
        """Return the memories in a namespace most relevant to a query.

        Raises:
            AgentMemoryError: If the store could not be searched.
        """
        if not query.strip():
            return []
        try:
            embedding = await self._embedding_model.embed_query(query)
        except EmbeddingError as error:
            msg = f"could not embed a recall query: {error}"
            raise AgentMemoryError(msg) from error

        statement = (
            select(AgentMemoryRow)
            .where(
                AgentMemoryRow.tenant_id == self._tenant_id,
                AgentMemoryRow.namespace == list(namespace),
            )
            .order_by(AgentMemoryRow.embedding.cosine_distance(list(embedding.values)))
            .limit(limit)
        )
        try:
            async with self._engine.connect() as connection:
                rows = (await connection.execute(statement)).mappings().all()
        except SQLAlchemyError as error:
            msg = f"could not recall memories under {list(namespace)}: {error}"
            raise AgentMemoryError(msg) from error
        return [{str(key): str(item) for key, item in row["content"].items()} for row in rows]
