"""Contract for the DocumentRepository port."""

import pytest

from paimon.domain.entities import Document
from paimon.domain.ports import DocumentRepository

TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"


DEFAULT_FINGERPRINT = "chunk:max=512,overlap=64,min=16|embed:m@1024"


def document(document_id: str = "doc-1", **overrides: object) -> Document:
    """Build a document for use in a contract test."""
    fields: dict[str, object] = {
        "document_id": document_id,
        "tenant_id": TENANT,
        "source_uri": "https://example.test/runbook.md",
        "title": "Node maintenance",
        "text": "# Node maintenance\n\nCordon the node first.",
        "content_hash": "hash-1",
        "media_type": "text/markdown",
        "pipeline_fingerprint": DEFAULT_FINGERPRINT,
        "metadata": {},
    }
    fields.update(overrides)
    return Document(**fields)  # type: ignore[arg-type]


class DocumentRepositoryContract:
    """Every DocumentRepository adapter must pass these."""

    @pytest.fixture
    def repository(self) -> DocumentRepository:
        """Supplied by the subclass, empty and ready to write to."""
        raise NotImplementedError

    async def test_a_saved_document_comes_back(self, repository: DocumentRepository) -> None:
        await repository.save(document())
        loaded = await repository.get(TENANT, "doc-1")

        assert loaded is not None
        assert loaded.text == document().text
        assert loaded.content_hash == "hash-1"

    async def test_the_text_survives_verbatim(self, repository: DocumentRepository) -> None:
        """Chunk offsets index into this text. A store that trims or re-encodes it
        shifts every citation in the document."""
        text = "# Title\n\n  indented line  \n\nTrailing paragraph.\n\ttabbed"
        await repository.save(document(text=text))
        loaded = await repository.get(TENANT, "doc-1")

        assert loaded is not None
        assert loaded.text == text

    async def test_the_pipeline_fingerprint_round_trips(
        self, repository: DocumentRepository
    ) -> None:
        """It decides whether a document needs re-chunking. A store that drops it
        makes every re-ingestion look unnecessary."""
        await repository.save(document())
        loaded = await repository.get(TENANT, "doc-1")

        assert loaded is not None
        assert loaded.pipeline_fingerprint == DEFAULT_FINGERPRINT

    async def test_metadata_round_trips(self, repository: DocumentRepository) -> None:
        await repository.save(document(metadata={"team": "platform", "source": "github"}))
        loaded = await repository.get(TENANT, "doc-1")

        assert loaded is not None
        assert dict(loaded.metadata) == {"team": "platform", "source": "github"}

    async def test_an_unknown_document_is_none(self, repository: DocumentRepository) -> None:
        assert await repository.get(TENANT, "never-stored") is None

    async def test_saving_again_replaces(self, repository: DocumentRepository) -> None:
        """Re-ingestion is routine, so a second save is an update rather than a
        duplicate or an error."""
        await repository.save(document())
        await repository.save(
            document(text="# Retired\n\nThis procedure is gone.", content_hash="hash-2")
        )
        loaded = await repository.get(TENANT, "doc-1")

        assert loaded is not None
        assert loaded.content_hash == "hash-2"
        assert "Retired" in loaded.text

    async def test_another_tenant_cannot_read_it(self, repository: DocumentRepository) -> None:
        await repository.save(document())
        assert await repository.get(OTHER_TENANT, "doc-1") is None

    async def test_the_same_id_in_two_tenants_stays_separate(
        self, repository: DocumentRepository
    ) -> None:
        await repository.save(document(text="# Tenant A\n\nBody."))
        await repository.save(
            document(tenant_id=OTHER_TENANT, text="# Tenant B\n\nBody.", content_hash="hash-b")
        )

        first = await repository.get(TENANT, "doc-1")
        second = await repository.get(OTHER_TENANT, "doc-1")
        assert first is not None
        assert second is not None
        assert "Tenant A" in first.text
        assert "Tenant B" in second.text

    async def test_deleting_reports_whether_anything_went(
        self, repository: DocumentRepository
    ) -> None:
        await repository.save(document())

        assert await repository.delete(TENANT, "doc-1") is True
        assert await repository.delete(TENANT, "doc-1") is False
        assert await repository.get(TENANT, "doc-1") is None
