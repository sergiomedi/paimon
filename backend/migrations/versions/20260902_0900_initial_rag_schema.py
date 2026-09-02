"""Initial RAG schema: documents, chunks, and their indexes.

Revision ID: 20260902_0900
Revises:
Created: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "20260902_0900"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIMENSIONS = 1024


def upgrade() -> None:
    """Create the extension, the tables and their indexes."""
    # Idempotent so that a database provisioned from the pgvector image, whose
    # init script already enables it, migrates without special-casing.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # PostgreSQL only accepts IMMUTABLE expressions in a generated column, and
    # array_to_string is merely STABLE: for a general anyarray its result depends
    # on the element type's output function, which need not be immutable. For
    # text[] it is — text's output function is the identity — so this wrapper
    # restricts the signature to text[] and declares what is true of that case.
    # Widening the signature would make the declaration a lie to the planner.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION paimon_array_to_string(text[], text)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        STRICT
        AS $$ SELECT array_to_string($1, $2) $$
        """
    )

    op.create_table(
        "documents",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("document_id", sa.String(length=256), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        # Not "metadata": that name collides with SQLAlchemy's MetaData
        # attribute on a declarative class.
        sa.Column(
            "doc_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("tenant_id", "document_id"),
    )
    # Ingestion asks "has this content already been stored" on every run.
    op.create_index("ix_documents_content_hash", "documents", ["tenant_id", "content_hash"])

    op.create_table(
        "chunks",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("chunk_id", sa.String(length=320), nullable=False),
        sa.Column("document_id", sa.String(length=256), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("heading_path", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("start_char", sa.Integer(), nullable=False),
        sa.Column("end_char", sa.Integer(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column("embedding_model", sa.String(length=128), nullable=False),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', paimon_array_to_string(heading_path, ' ') || ' ' || text)",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tenant_id", "chunk_id"),
        # No foreign key to documents: chunks live behind the VectorStore port and
        # documents behind DocumentRepository, and one supported configuration
        # puts them in different systems (Azure AI Search and PostgreSQL). A
        # schema that depended on referential integrity between them could not be
        # deployed that way.
    )
    op.create_index("ix_chunks_document", "chunks", ["tenant_id", "document_id"])
    op.create_index("ix_chunks_search_vector", "chunks", ["search_vector"], postgresql_using="gin")
    # Cosine, to match the similarity the domain computes. Built here because the
    # table is empty; on an already-loaded table it is faster to load first and
    # index afterwards.
    op.create_index(
        "ix_chunks_embedding_hnsw",
        "chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    """Drop the tables.

    The extension is left in place: it may predate this migration, and other
    schemas in the same database may depend on it.
    """
    op.drop_table("chunks")
    op.drop_table("documents")
    op.execute("DROP FUNCTION IF EXISTS paimon_array_to_string(text[], text)")
