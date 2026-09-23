"""How agent state is written into a graph checkpoint, and read back unchanged.

The framework's default serializer accepts any type and warns that it will not
forever: deserializing an arbitrary class from a checkpoint is code execution
for anyone who can write to that database. It offers a strict mode with an
explicit allowlist, and this is that allowlist.

Naming the types the platform actually stores does two things. It closes the
hole — nothing else can be revived out of a checkpoint — and it makes the
state's serialized surface a decision someone has to make on purpose: adding a
type to the graph's state means adding it here, and being asked why it belongs
in a checkpoint at all.

**It also repairs what the encoding loses.** JSON has one sequence type, so a
tuple is written as a list and comes back as one. Every domain type here
declares tuples, so a revived value was of a type its own annotation denied —
``Chunk.heading_path`` came back a list, and a revived chunk therefore compared
unequal to the chunk that was stored. That is not cosmetic: a resumed postmortem
run reloads its citations from a checkpoint, and a citation that is not equal to
the one written is one nothing downstream can match against what it cited.

Repairing it here is the single place worth doing it. The alternative — a
``__post_init__`` on every domain type, coercing sequences it should never have
received — would spread a property of *this encoding* across nine classes that
have nothing to do with it, and would still be wrong for the tenth one somebody
adds later.
"""

from dataclasses import fields, is_dataclass, replace
from functools import cache
from typing import Any, get_origin, get_type_hints

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

#: The domain types that appear inside an AgentState, by module and name. Frozen
#: dataclasses of plain values, all of them: nothing here holds a connection, a
#: file handle or a callable, which is what makes them safe to revive.
ALLOWED_MODULES: tuple[tuple[str, ...], ...] = (
    ("paimon.domain.entities.document", "Chunk"),
    ("paimon.domain.entities.agent", "AgentStep"),
    ("paimon.domain.entities.agent", "RunStatus"),
    ("paimon.domain.value_objects.citation", "Citation"),
    # An agent that chooses its own next step carries its conversation in the
    # state, so a checkpoint of such a run contains the conversation. These five
    # are what that is made of. Message and ToolCall are the only types here
    # that hold text a *model* produced rather than text the platform wrote,
    # which is worth knowing when deciding who may read a checkpoint.
    ("paimon.domain.agents.transcript", "Transcript"),
    ("paimon.domain.agents.transcript", "SeenCall"),
    ("paimon.domain.agents.transcript", "StopReason"),
    ("paimon.domain.ports.chat", "Message"),
    ("paimon.domain.ports.chat", "ToolCall"),
)


@cache
def _tuple_fields(cls: type) -> frozenset[str]:
    """Which of a dataclass's fields are declared as tuples.

    Cached per class: the annotations are fixed for the life of the process, and
    resolving them involves importing the modules they name.

    ``get_type_hints`` rather than reading ``field.type``, because from ``from
    __future__ import annotations`` or a quoted forward reference the raw
    annotation is a string, and a string never matches ``tuple``.
    """
    try:
        hints = get_type_hints(cls)
    except (NameError, TypeError):  # pragma: no cover - an unresolvable annotation
        return frozenset()
    return frozenset(
        field.name
        for field in fields(cls)
        if get_origin(hints.get(field.name)) is tuple or hints.get(field.name) is tuple
    )


def restore(value: Any) -> Any:
    """Put back the tuples the encoding turned into lists.

    Walks the revived object graph and rebuilds each dataclass whose
    tuple-declared fields came back as lists. Recursive, because the values
    inside those lists are themselves dataclasses with the same problem — a
    ``Transcript`` holds ``Message`` objects that hold ``ToolCall`` objects.

    A bare list that is not a dataclass field is left alone. Nothing in the
    payload says whether it was written as a list or a tuple, and guessing would
    turn every list the platform legitimately stores into something else.
    """
    if is_dataclass(value) and not isinstance(value, type):
        wanted = _tuple_fields(type(value))
        changes: dict[str, Any] = {}
        for field in fields(value):
            current = getattr(value, field.name)
            restored = restore(current)
            if field.name in wanted and isinstance(restored, list):
                restored = tuple(restored)
            if restored is not current:
                changes[field.name] = restored
        return replace(value, **changes) if changes else value
    if isinstance(value, list):
        return [restore(item) for item in value]
    if isinstance(value, tuple):
        return tuple(restore(item) for item in value)
    if isinstance(value, dict):
        return {key: restore(item) for key, item in value.items()}
    return value


class PaimonSerializer(JsonPlusSerializer):
    """The framework's strict serializer, with the tuples put back.

    Subclassed rather than wrapped because the checkpointer takes a serializer
    and calls more of its surface than this platform does; a wrapper would have
    to forward every method the framework might add, and the day it added one
    the failure would be an attribute error inside a resume.
    """

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        """Decode a checkpointed value, restoring the types it declares."""
        return restore(super().loads_typed(data))


def build_serializer() -> PaimonSerializer:
    """Return the serializer the graph checkpointer should use."""
    return PaimonSerializer(allowed_msgpack_modules=ALLOWED_MODULES)
