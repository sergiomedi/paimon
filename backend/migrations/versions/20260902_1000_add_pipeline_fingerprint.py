"""Record which pipeline produced a document's chunks.

Revision ID: 20260902_1000
Revises: 20260902_0900
Created: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_1000"
down_revision: str | None = "20260902_0900"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the fingerprint column.

    Defaults to the empty string rather than being nullable: existing rows then
    read as "produced by an unknown pipeline", which never matches the current
    one, so they are reindexed exactly once and afterwards behave normally.
    """
    op.add_column(
        "documents",
        sa.Column(
            "pipeline_fingerprint",
            sa.String(length=256),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    """Drop the fingerprint column."""
    op.drop_column("documents", "pipeline_fingerprint")
