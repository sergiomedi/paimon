"""The state an agent graph carries, and how concurrent writes to it merge.

This module is the reason ADR-0015 exists. It is a plain dataclass with plain
functions, and it imports no orchestration framework: LangGraph accepts a
dataclass as a graph's state schema, and ``Annotated`` is stdlib typing, so the
reducers below are declared here and read by the adapter without this package
ever knowing what a ``StateGraph`` is.

What that buys: a node is ``async def node(state) -> StateUpdate``, so every node
body can be called and awaited in a unit test. A test that needs a graph in order
to observe a node is a test of the framework, not of the node.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, TypedDict

from paimon.domain.entities import AgentStep, Chunk
from paimon.domain.value_objects import Citation


def append_steps(left: Sequence[AgentStep], right: Sequence[AgentStep]) -> tuple[AgentStep, ...]:
    """Concatenate step records.

    The default behaviour for a state field is replacement, which is wrong for a
    trace: two branches running in parallel would each overwrite the other's
    record, and the run would end up remembering only whichever finished last.
    """
    return (*left, *right)


def _pair(value: Sequence[int]) -> tuple[int, int]:
    """Read a token pair from a sequence that may be shorter than two.

    Not defensiveness for its own sake. An orchestrator initialises an
    aggregating channel from the zero value of the annotated type, and for a
    tuple that is ``()`` rather than ``(0, 0)`` — so the first reduction of every
    run arrives with an empty left operand. A reducer that assumes two elements
    fails on the first model call of every run, and the traceback names this
    module rather than the framework that supplied the value.
    """
    return (value[0] if len(value) > 0 else 0, value[1] if len(value) > 1 else 0)


def add_usage(left: Sequence[int], right: Sequence[int]) -> tuple[int, int]:
    """Add two (input, output) token pairs.

    Usage accumulates rather than replaces, so ``state.usage`` is what the run
    has spent so far and a branch can be taken on it — a graph deciding whether
    it can afford another model call needs the running total, not the last
    node's share.
    """
    left_in, left_out = _pair(left)
    right_in, right_out = _pair(right)
    return (left_in + right_in, left_out + right_out)


def combine_failures(left: str, right: str) -> str:
    """Join the reasons a run failed, keeping each one once.

    A reducer rather than the default replacement, and the reason is worth
    stating because it took a real run to find. Any node can fail, and the
    adapter records the failure as state — so two nodes running concurrently can
    both fail, and two writes to one field in one step is an error the
    orchestrator refuses outright. Retrieval that runs two framings in parallel
    against an unreachable provider fails twice by construction, which makes this
    the ordinary case rather than the exotic one.

    Both reasons are kept: when two branches fail differently, the second is
    usually what explains the first.
    """
    reasons = [reason for reason in (left, right) if reason]
    return "; ".join(dict.fromkeys(reasons))


def merge_evidence(left: Sequence[Chunk], right: Sequence[Chunk]) -> tuple[Chunk, ...]:
    """Combine retrieved chunks, keeping the first occurrence and dropping repeats.

    Parallel branches routinely retrieve the same chunk. Deduplicating here
    rather than inside each node keeps the invariant in one place, and preserving
    order keeps the merge deterministic — which is what makes a run reproducible
    and therefore evaluable.
    """
    seen: set[str] = set()
    merged: list[Chunk] = []
    for chunk in (*left, *right):
        if chunk.chunk_id in seen:
            continue
        seen.add(chunk.chunk_id)
        merged.append(chunk)
    return tuple(merged)


@dataclass(slots=True)
class AgentState:
    """What flows between the nodes of an agent graph.

    Mutable, unlike the domain entities, because a graph state is precisely the
    thing that accumulates. Nodes never mutate it: they return a
    :class:`StateUpdate` and the reducers above decide how concurrent writes to
    the same field combine.

    Attributes:
        question: What the run was asked to do.
        tenant_id: Whose material the run may read.
        evidence: Chunks retrieved so far, deduplicated by the reducer.
        citations: Sources the draft actually referred to.
        draft: The answer under construction.
        steps: Append-only record of the nodes that have run.
        usage: Input and output tokens the run has spent so far, summed by the
            reducer. A node that calls a model reports its own share and the
            adapter attributes it to that node's step.
        notes: A scratchpad. What one part of a run worked out and a later part
            needs, when the two are separated by nodes that neither produce nor
            consume it — a sub-agent's conclusion, read by the node that composes
            over it before that node overwrites ``draft``.
        awaiting: What a human is being asked to decide. A node sets it to
            suspend the run; the adapter is what actually stops, because
            interrupting is a property of the runtime and a node that knew how
            to do it would be a node that needed one to be tested.
        decision: What the person answered, once they have. Empty until then, so
            a node can tell "not asked yet" from "asked and answered".
        failure: Why the run failed, when it did. Joined by the reducer, because
            concurrent branches can fail independently and separately.
    """

    # Defaulted, not because a run without a question is meaningful, but because
    # an orchestrator resuming from a checkpoint constructs the state schema with
    # no arguments before filling it in. A required field turns every resumed run
    # into a TypeError raised from inside the framework. The invariant is kept
    # where it can be: stream() always builds this with both, and AgentRun — the
    # record a person actually reads — refuses a blank tenant outright.
    question: str = ""
    tenant_id: str = ""
    evidence: Annotated[tuple[Chunk, ...], merge_evidence] = ()
    citations: tuple[Citation, ...] = ()
    draft: str = ""
    notes: str = ""
    steps: Annotated[tuple[AgentStep, ...], append_steps] = ()
    usage: Annotated[tuple[int, int], add_usage] = (0, 0)
    awaiting: str = ""
    decision: str = ""
    failure: Annotated[str, combine_failures] = ""

    @property
    def grounded(self) -> bool:
        """Whether the draft rests on anything that was retrieved."""
        return bool(self.citations)


class StateUpdate(TypedDict, total=False):
    """What a node returns: the fields it changed, and nothing else.

    Partial by design. A node that returns the whole state has to decide what to
    do about fields it never looked at, and the honest answer — leave them alone
    — is what a partial update says without having to say it. ``total=False``
    means the type checker accepts any subset and rejects a key that is not a
    state field, which catches the typo that would otherwise be silently
    discarded at runtime.
    """

    question: str
    tenant_id: str
    evidence: tuple[Chunk, ...]
    citations: tuple[Citation, ...]
    draft: str
    notes: str
    steps: tuple[AgentStep, ...]
    usage: tuple[int, int]
    awaiting: str
    decision: str
    failure: str
