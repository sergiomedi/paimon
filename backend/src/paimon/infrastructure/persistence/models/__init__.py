"""SQLAlchemy models.

These never leave this package. Repositories map them to domain entities at the
boundary, which is what stops the database schema from quietly becoming the
domain model.
"""

from paimon.infrastructure.persistence.models.base import Base
from paimon.infrastructure.persistence.models.rag import ChunkRow, DocumentRow

__all__ = ["Base", "ChunkRow", "DocumentRow"]
