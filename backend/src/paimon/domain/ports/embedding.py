"""Port for turning text into vectors."""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from paimon.domain.value_objects import Embedding


@runtime_checkable
class EmbeddingModel(Protocol):
    """Produces embeddings for indexing and for querying.

    Documents and queries are embedded through separate methods because several
    families of model are asymmetric: they expect an instruction prefix on the
    query side and none on the document side, and using one path for both quietly
    costs retrieval quality. A symmetric model implements both identically, which
    costs nothing.
    """

    @property
    def model_id(self) -> str:
        """Identifier written onto every embedding this model produces."""
        ...

    @property
    def dimensions(self) -> int:
        """Dimensionality of the vectors this model produces."""
        ...

    async def embed_documents(self, texts: Sequence[str]) -> list[Embedding]:
        """Embed text destined for the index.

        Args:
            texts: Texts to embed, in order.

        Returns:
            One embedding per input, in the same order.

        Raises:
            EmbeddingError: If the provider could not produce the embeddings.
        """
        ...

    async def embed_query(self, text: str) -> Embedding:
        """Embed a search query.

        Args:
            text: The query.

        Returns:
            The query's embedding.

        Raises:
            EmbeddingError: If the provider could not produce the embedding.
        """
        ...
