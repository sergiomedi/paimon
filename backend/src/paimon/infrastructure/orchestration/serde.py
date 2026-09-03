"""How agent state is written into a graph checkpoint.

The framework's default serializer accepts any type and warns that it will not
forever: deserializing an arbitrary class from a checkpoint is code execution
for anyone who can write to that database. It offers a strict mode with an
explicit allowlist, and this is that allowlist.

Naming the four types the platform actually stores does two things. It closes
the hole — nothing else can be revived out of a checkpoint — and it makes the
state's serialized surface a decision someone has to make on purpose: adding a
type to the graph's state means adding it here, and being asked why it belongs
in a checkpoint at all.
"""

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

#: The domain types that appear inside an AgentState, by module and name. Frozen
#: dataclasses of plain values, all of them: nothing here holds a connection, a
#: file handle or a callable, which is what makes them safe to revive.
ALLOWED_MODULES: tuple[tuple[str, ...], ...] = (
    ("paimon.domain.entities.document", "Chunk"),
    ("paimon.domain.entities.agent", "AgentStep"),
    ("paimon.domain.entities.agent", "RunStatus"),
    ("paimon.domain.value_objects.citation", "Citation"),
)


def build_serializer() -> JsonPlusSerializer:
    """Return the serializer the graph checkpointer should use."""
    return JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_MODULES)
