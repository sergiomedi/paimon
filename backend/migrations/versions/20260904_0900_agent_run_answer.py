"""Keep what an agent run produced, not only what it did.

Revision ID: 20260904_0900
Revises: 20260903_0900
Created: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_0900"
down_revision: str | None = "20260903_0900"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the answer column.

    Defaults to the empty string rather than being nullable: runs recorded before
    this migration produced an answer that was never kept, and "" says that
    honestly, where NULL would invite a caller to guess whether the run failed.
    """
    op.add_column(
        "agent_runs",
        sa.Column("answer", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    """Drop the answer column."""
    op.drop_column("agent_runs", "answer")
