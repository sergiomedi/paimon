"""Recording measurements alongside the spans.

The decorators already hold the numbers — how long a call took, what it returned,
what it cost in tokens — so producing the metrics from the same place costs
nothing beyond the call to record them. It also guarantees the two agree: a
dashboard whose totals disagree with its traces is a dashboard nobody trusts
twice, and the usual cause is two pieces of code counting the same thing.

Instruments are created once per process and cached. Creating one per call is
permitted by the API and is wasteful, and the SDK warns about duplicates.

This module sits beside the tracing helpers rather than in ``infrastructure``,
because the MCP interface records tool executions through it and an interface
reaching into infrastructure is exactly what the layering contracts forbid.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache

from opentelemetry.metrics import Histogram

from paimon.config import PricingSettings
from paimon.observability.genai import OPERATION, PROVIDER, Operation, Provider
from paimon.observability.metrics import (
    AGENT_DURATION,
    AGENT_INFERENCE_CALLS,
    CURRENCY,
    ESTIMATED_COST,
    OPERATION_DURATION,
    PRICE_REVISION,
    TOKEN_TYPE,
    TOKEN_USAGE,
    TOOL_DURATION,
    get_meter,
)

MODEL = "gen_ai.request.model"
ERROR_TYPE = "error.type"
AGENT = "gen_ai.agent.name"
TOOL = "gen_ai.tool.name"


@dataclass(frozen=True, slots=True)
class Instruments:
    """The histograms this platform records into."""

    tokens: Histogram
    duration: Histogram
    cost: Histogram
    agent_duration: Histogram
    agent_inference_calls: Histogram
    tool_duration: Histogram


@lru_cache(maxsize=1)
def instruments() -> Instruments:
    """Create the instruments once for the process."""
    meter = get_meter()
    return Instruments(
        tokens=meter.create_histogram(
            TOKEN_USAGE, unit="{token}", description="Tokens consumed by a model call."
        ),
        duration=meter.create_histogram(
            OPERATION_DURATION, unit="s", description="Duration of a model call."
        ),
        cost=meter.create_histogram(
            ESTIMATED_COST,
            unit="1",
            description=(
                "Estimated cost of a model call, from token counts and a configured "
                "price list. Not measured, and not the provider's invoice."
            ),
        ),
        agent_duration=meter.create_histogram(
            AGENT_DURATION, unit="s", description="Duration of an agent run."
        ),
        agent_inference_calls=meter.create_histogram(
            AGENT_INFERENCE_CALLS,
            unit="{inference_call}",
            description="Model calls made during one agent run.",
        ),
        tool_duration=meter.create_histogram(
            TOOL_DURATION, unit="s", description="Duration of one tool execution."
        ),
    )


@dataclass(slots=True)
class _Counter:
    """A count shared with everything running inside a run."""

    value: int = 0


#: Counts the model calls made inside one agent run.
#:
#: A context variable holding a **mutable** object, and that detail is the whole
#: mechanism. The orchestrator runs nodes in separate tasks, and asyncio copies
#: the context into each one — so a child that rebound the variable would count
#: into its own copy and the parent would see nothing. Sharing one object means
#: two branches retrieving concurrently both count into the run that spawned
#: them.
_inference_calls: ContextVar[_Counter | None] = ContextVar("paimon_inference_calls", default=None)


@contextmanager
def measured_run(agent: str) -> Iterator[None]:
    """Time an agent run and count the model calls made inside it.

    The duration is recorded whether the run succeeded, failed or stopped for a
    person: a run that suspends after nine seconds took nine seconds, and a
    dashboard counting only completed runs would report an agent as fast because
    its slow ones never finish.

    The call count is the number a cost conversation actually turns on. Two
    agents with the same duration and a different count are two different bills,
    and nothing else in a run record says which is which.
    """
    started = time.perf_counter()
    counter = _Counter()
    token = _inference_calls.set(counter)
    try:
        yield
    finally:
        _inference_calls.reset(token)
        instruments().agent_duration.record(time.perf_counter() - started, {AGENT: agent})
        instruments().agent_inference_calls.record(counter.value, {AGENT: agent})


@contextmanager
def measured_tool(tool: str) -> Iterator[None]:
    """Time one tool execution, recording failures as well as successes."""
    started = time.perf_counter()
    error: str | None = None
    try:
        yield
    except Exception as failure:
        error = type(failure).__qualname__
        raise
    finally:
        attributes = {TOOL: tool} if error is None else {TOOL: tool, ERROR_TYPE: error}
        instruments().tool_duration.record(time.perf_counter() - started, attributes)


@contextmanager
def measured(operation: Operation, provider: Provider, model: str) -> Iterator["Measurement"]:
    """Time an operation and record its duration, however it ends.

    The duration is recorded on failure too, tagged with the error type. A
    provider that times out at thirty seconds on every call would otherwise
    appear in a dashboard as no latency at all, because none of its calls
    finished — which is the opposite of what an operator needs to see.
    """
    started = time.perf_counter()
    measurement = Measurement(operation=operation, provider=provider, model=model)
    try:
        yield measurement
    except Exception as error:
        _record_duration(measurement, time.perf_counter() - started, type(error).__qualname__)
        raise
    _record_duration(measurement, time.perf_counter() - started, None)


@dataclass(slots=True)
class Measurement:
    """One operation in progress, and what it turned out to cost."""

    operation: Operation
    provider: Provider
    model: str

    def record_tokens(
        self, *, input_tokens: int, output_tokens: int, pricing: PricingSettings | None = None
    ) -> None:
        """Record what a call consumed, and what that is worth if it is priced.

        Args:
            input_tokens: Tokens the request carried.
            output_tokens: Tokens the answer carried.
            pricing: The price list, when this deployment has one. A model absent
                from it produces no cost measurement rather than a zero: zero is
                a claim about what something cost, and silence is the truth.
        """
        if (counter := _inference_calls.get()) is not None:
            counter.value += 1

        common = {OPERATION: self.operation.value, PROVIDER: self.provider.value}
        instrument = instruments()
        instrument.tokens.record(input_tokens, {**common, TOKEN_TYPE: "input", MODEL: self.model})
        instrument.tokens.record(output_tokens, {**common, TOKEN_TYPE: "output", MODEL: self.model})

        if pricing is None:
            return
        estimated = pricing.cost(self.model, input_tokens=input_tokens, output_tokens=output_tokens)
        if estimated is None:
            return
        instrument.cost.record(
            estimated,
            {
                **common,
                MODEL: self.model,
                CURRENCY: pricing.currency,
                # The revision travels with every measurement, so a figure can be
                # traced back to the prices that produced it. Without it, last
                # month's numbers become uninterpretable the moment the table
                # changes — and the table changes.
                PRICE_REVISION: pricing.revision,
            },
        )


def _record_duration(measurement: Measurement, seconds: float, error: str | None) -> None:
    """Record how long an operation took, and how it ended."""
    attributes: dict[str, str] = {
        OPERATION: measurement.operation.value,
        PROVIDER: measurement.provider.value,
        MODEL: measurement.model,
    }
    if error is not None:
        attributes[ERROR_TYPE] = error
    instruments().duration.record(seconds, attributes)


def reset_instruments() -> None:
    """Forget the cached instruments.

    Process-global state, cached for the life of the process. A test that
    installs its own meter provider needs the next call to build against it
    rather than against the one the previous test cached.
    """
    instruments.cache_clear()


__all__: list[str] = [
    "AGENT",
    "ERROR_TYPE",
    "MODEL",
    "TOOL",
    "Instruments",
    "Measurement",
    "instruments",
    "measured",
    "measured_run",
    "measured_tool",
    "reset_instruments",
]
