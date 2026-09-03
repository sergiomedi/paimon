"""Tables backing agent runs and what agents remember between them."""

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import ARRAY, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from paimon.infrastructure.persistence.models.base import Base
from paimon.infrastructure.persistence.models.rag import EMBEDDING_DIMENSIONS


class AgentRunRow(Base):
    """One execution of one agent.

    Keyed by thread rather than by a surrogate id, because the thread is how a
    run is addressed for resumption: a run that cannot be named cannot be
    continued.

    The steps are stored as one JSONB document rather than as a child table. They
    are written together, read together and never queried individually, so a
    second table would buy a join and nothing else. The cost is that a step
    cannot be indexed on its own, which is a query nothing in the platform makes.
    """

    __tablename__ = "agent_runs"

    thread_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    agent: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Listing a tenant's runs newest first is the only query this table
        # serves, so it is the only index it gets.
        Index("ix_agent_runs_tenant_started", "tenant_id", started_at.desc()),
    )


class AgentMemoryRow(Base):
    """Something an agent learned in one run and may want in another.

    The namespace is an array rather than a delimited string so that a prefix
    query stays an array operation: a delimiter is a character that eventually
    appears inside a segment, and the day it does the boundary moves silently.
    """

    __tablename__ = "agent_memories"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    namespace: Mapped[list[str]] = mapped_column(ARRAY(Text), primary_key=True)
    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # What the memory is *about*, embedded. Recall is a similarity search, so
    # the text that was embedded is kept: an embedding whose source is unknown
    # cannot be recomputed when the model changes.
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "ix_agent_memories_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_agent_memories_namespace", "tenant_id", "namespace"),
    )
