"""PostgreSQL adapter for the AgentCheckpointer port.

Written on SQLAlchemy and asyncpg, the drivers the platform already uses, rather
than on the orchestration framework's own PostgreSQL checkpointer. The two are
not the same thing: this persists the run as the platform's domain describes it —
named steps, timestamps, token counts, a status an operator can act on — while
the framework's checkpointer persists graph state for resumption. Batch 6 needs
the second; nothing here does, and adopting a second database driver to store
records this schema already describes would be paying for a dependency in
advance.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from paimon.domain.entities import AgentRun, AgentStep, RunStatus
from paimon.domain.errors import CheckpointError
from paimon.domain.value_objects import Citation
from paimon.infrastructure.persistence.models import AgentRunRow


def _encode(step: AgentStep) -> dict[str, Any]:
    return {
        "name": step.name,
        "summary": step.summary,
        "started_at": step.started_at.isoformat(),
        "finished_at": step.finished_at.isoformat(),
        "input_tokens": step.input_tokens,
        "output_tokens": step.output_tokens,
        "details": dict(step.details),
    }


def _decode(raw: dict[str, Any]) -> AgentStep:
    return AgentStep(
        name=raw["name"],
        summary=raw["summary"],
        started_at=datetime.fromisoformat(raw["started_at"]),
        finished_at=datetime.fromisoformat(raw["finished_at"]),
        input_tokens=raw.get("input_tokens", 0),
        output_tokens=raw.get("output_tokens", 0),
        details=dict(raw.get("details", {})),
    )


def _encode_citation(citation: Citation) -> dict[str, Any]:
    return {
        "marker": citation.marker,
        "document_id": citation.document_id,
        "chunk_id": citation.chunk_id,
        "source_uri": citation.source_uri,
        "title": citation.title,
        "heading_path": list(citation.heading_path),
        "start_char": citation.start_char,
        "end_char": citation.end_char,
        "quote": citation.quote,
    }


def _decode_citation(raw: dict[str, Any]) -> Citation:
    return Citation(
        marker=raw["marker"],
        document_id=raw["document_id"],
        chunk_id=raw["chunk_id"],
        source_uri=raw["source_uri"],
        title=raw["title"],
        heading_path=tuple(raw.get("heading_path", ())),
        start_char=raw["start_char"],
        end_char=raw["end_char"],
        quote=raw["quote"],
    )


def _run(row: Any) -> AgentRun:
    started_at = row["started_at"]
    return AgentRun(
        thread_id=row["thread_id"],
        agent=row["agent"],
        tenant_id=row["tenant_id"],
        status=RunStatus(row["status"]),
        answer=row["answer"],
        citations=tuple(_decode_citation(item) for item in row["citations"]),
        steps=tuple(_decode(item) for item in row["steps"]),
        # A column declared with a timezone can still read back naive through
        # some drivers, and AgentStep refuses naive timestamps. Assuming UTC
        # here is safe because that is what was written.
        started_at=started_at if started_at.tzinfo else started_at.replace(tzinfo=UTC),
    )


class PostgresCheckpointer:
    """Stores agent runs in PostgreSQL."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Initialise the checkpointer.

        Args:
            engine: Engine whose pool connections are borrowed from.
        """
        self._engine = engine

    async def save(self, run: AgentRun) -> None:
        """Insert or replace a run.

        Upsert because this is called after every step of a live run, which is
        the point: a process that dies mid-run leaves a record of how far it got.

        Raises:
            CheckpointError: If the run could not be persisted.
        """
        values = {
            "thread_id": run.thread_id,
            "tenant_id": run.tenant_id,
            "agent": run.agent,
            "status": str(run.status),
            "answer": run.answer,
            "citations": [_encode_citation(citation) for citation in run.citations],
            "steps": [_encode(step) for step in run.steps],
            "started_at": run.started_at,
        }
        statement = insert(AgentRunRow).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[AgentRunRow.thread_id],
            set_={
                "status": statement.excluded["status"],
                "answer": statement.excluded["answer"],
                "citations": statement.excluded["citations"],
                "steps": statement.excluded["steps"],
                "updated_at": datetime.now(UTC),
            },
        )
        try:
            async with self._engine.begin() as connection:
                await connection.execute(statement)
        except SQLAlchemyError as error:
            msg = f"could not checkpoint run '{run.thread_id}': {error}"
            raise CheckpointError(msg) from error

    async def load(self, thread_id: str) -> AgentRun | None:
        """Return a run, or None when the thread is unknown.

        Raises:
            CheckpointError: If the store could not be read.
        """
        statement = select(AgentRunRow).where(AgentRunRow.thread_id == thread_id)
        try:
            async with self._engine.connect() as connection:
                row = (await connection.execute(statement)).mappings().one_or_none()
        except SQLAlchemyError as error:
            msg = f"could not read run '{thread_id}': {error}"
            raise CheckpointError(msg) from error
        return None if row is None else _run(row)

    async def list_runs(self, tenant_id: str, *, limit: int = 50) -> Sequence[AgentRun]:
        """Return a tenant's runs, most recently started first.

        Raises:
            CheckpointError: If the store could not be read.
        """
        statement = (
            select(AgentRunRow)
            .where(AgentRunRow.tenant_id == tenant_id)
            .order_by(AgentRunRow.started_at.desc(), AgentRunRow.thread_id)
            .limit(limit)
        )
        try:
            async with self._engine.connect() as connection:
                rows = (await connection.execute(statement)).mappings().all()
        except SQLAlchemyError as error:
            msg = f"could not list runs for '{tenant_id}': {error}"
            raise CheckpointError(msg) from error
        return [_run(row) for row in rows]
