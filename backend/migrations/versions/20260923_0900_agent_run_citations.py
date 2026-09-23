"""Keep what an answer rests on, not only the answer.

Revision ID: 20260923_0900
Revises: 20260904_0900
Created: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260923_0900"
down_revision: str | None = "20260904_0900"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the citations column.

    An empty list rather than NULL, and the distinction is the same one the
    answer column drew. Runs recorded before this migration produced citations
    that were never kept; ``[]`` says "this record holds none", which is exactly
    true of them, where NULL would invite a caller to guess whether the run was
    ungrounded or merely old.

    It also means every existing row is readable the moment this lands, with no
    backfill: the adapter decodes ``[]`` into an empty tuple, and a run that
    cites nothing is a state the domain already has a meaning for.
    """
    op.add_column(
        "agent_runs",
        sa.Column(
            "citations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    """Drop the citations column."""
    op.drop_column("agent_runs", "citations")
