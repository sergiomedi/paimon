"""Persist agent runs, and what agents recall between them.

Revision ID: 20260903_0900
Revises: 20260902_1000
Created: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "20260903_0900"
down_revision: str | None = "20260902_1000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIMENSIONS = 1024


def upgrade() -> None:
    """Create the run and memory tables.

    The vector extension is already installed by the initial migration; this one
    only uses it. Repeating the CREATE EXTENSION would be harmless and would also
    make this migration look like it owns something it does not.
    """
    op.create_table(
        "agent_runs",
        sa.Column("thread_id", sa.String(length=256), primary_key=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("agent", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "steps", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_agent_runs_tenant_started",
        "agent_runs",
        ["tenant_id", sa.text("started_at DESC")],
    )

    op.create_table(
        "agent_memories",
        sa.Column("tenant_id", sa.String(length=128), primary_key=True),
        sa.Column("namespace", postgresql.ARRAY(sa.Text()), primary_key=True),
        sa.Column("key", sa.String(length=256), primary_key=True),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column("embedding_model", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_agent_memories_namespace", "agent_memories", ["tenant_id", "namespace"])
    # Cosine, matching the retrieval index: the same embedding model produces
    # both, and two distance functions over one model is two rankings that
    # disagree for no reason a reader could explain.
    op.create_index(
        "ix_agent_memories_embedding",
        "agent_memories",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    """Drop both tables."""
    op.drop_table("agent_memories")
    op.drop_table("agent_runs")
