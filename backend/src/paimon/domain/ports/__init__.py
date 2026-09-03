"""Ports: the interfaces the domain requires of the outside world.

Each port is a Protocol describing only what the application actually needs, not
the union of what every possible backend can do. Implementations live in the
infrastructure layer and are bound to these protocols at the composition root.
"""

from paimon.domain.ports.chat import ChatModel, Completion, Message, Role
from paimon.domain.ports.embedding import EmbeddingModel
from paimon.domain.ports.health import HealthProbe
from paimon.domain.ports.identity import IdentityProvider
from paimon.domain.ports.orchestration import (
    AgentCheckpointer,
    AgentMemory,
    AgentWorkflow,
    HumanInTheLoop,
)
from paimon.domain.ports.parsing import DocumentParser, ParsedDocument
from paimon.domain.ports.repository import DocumentRepository
from paimon.domain.ports.retrieval import (
    ChunkRecord,
    IndexDescriptor,
    NativeHybridSearch,
    SearchFilters,
    SearchHit,
    VectorStore,
)
from paimon.domain.ports.tokenization import TokenCounter

__all__ = [
    "AgentCheckpointer",
    "AgentMemory",
    "AgentWorkflow",
    "ChatModel",
    "ChunkRecord",
    "Completion",
    "DocumentParser",
    "DocumentRepository",
    "EmbeddingModel",
    "HealthProbe",
    "HumanInTheLoop",
    "IdentityProvider",
    "IndexDescriptor",
    "Message",
    "NativeHybridSearch",
    "ParsedDocument",
    "Role",
    "SearchFilters",
    "SearchHit",
    "TokenCounter",
    "VectorStore",
]
