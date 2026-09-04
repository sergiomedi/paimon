"""Measurements, against a real SDK reading into memory.

An in-memory reader rather than a mock exporter: what is being checked is that
the SDK aggregates what this platform records into the series a dashboard will
query, and only the SDK can answer that.
"""

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from pydantic import ValidationError

from paimon.config import Environment, MetricsSettings, ModelPrice, PricingSettings
from paimon.observability import metrics as metrics_module
from paimon.observability import recording
from paimon.observability.genai import Operation, Provider
from paimon.observability.metrics import (
    AGENT_DURATION,
    AGENT_INFERENCE_CALLS,
    ESTIMATED_COST,
    OPERATION_DURATION,
    PRICE_REVISION,
    TOKEN_BUCKETS,
    TOKEN_TYPE,
    TOKEN_USAGE,
    TOOL_DURATION,
    build_meter_provider,
)
from paimon.observability.recording import measured, measured_run, measured_tool
from paimon.observability.tracing import build_resource

ENDPOINT = "https://collector.example.test/v1/metrics"

PRICES = PricingSettings(
    currency="USD",
    revision="2026-09-01",
    models={"gpt-4o-mini": ModelPrice(input=0.15, output=0.60)},
)


@dataclass(frozen=True, slots=True)
class Recorded:
    """What the reader collected."""

    reader: InMemoryMetricReader

    def points(self, name: str) -> list[HistogramDataPoint]:
        collected = self.reader.get_metrics_data()
        if collected is None:
            return []
        return [
            point
            for resource in collected.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == name
            for point in metric.data.data_points
            if isinstance(point, HistogramDataPoint)
        ]

    def attributes(self, name: str) -> dict[str, object]:
        """The attributes of the first series recorded under a metric name."""
        return dict(self.points(name)[0].attributes or {})

    def names(self) -> set[str]:
        collected = self.reader.get_metrics_data()
        if collected is None:
            return set()
        return {
            metric.name
            for resource in collected.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        }


@pytest.fixture(autouse=True)
def recorded(monkeypatch: pytest.MonkeyPatch) -> Iterator[Recorded]:
    """A meter reading into memory, substituted for the platform's lookup.

    The process-wide meter provider, like the tracer provider, can be installed
    once. Substituting the lookup keeps the decision inside this module.
    """
    reader = InMemoryMetricReader()
    provider = MeterProvider(
        resource=Resource.create({"service.name": "test"}),
        metric_readers=[reader],
        views=metrics_module._views(),
    )
    # Patched where it is *used*, not where it is defined: recording.py imports
    # the name, so rebinding it on the metrics module would leave recording
    # holding the original.
    monkeypatch.setattr(recording, "get_meter", lambda: provider.get_meter("paimon"))
    recording.reset_instruments()
    yield Recorded(reader=reader)
    recording.reset_instruments()
    provider.shutdown()


class TestBuildingTheProvider:
    def test_metrics_are_off_unless_asked_for(self) -> None:
        assert (
            build_meter_provider(
                MetricsSettings(),
                resource=build_resource(
                    service_name="paimon-api",
                    service_version="1.2.3",
                    environment=Environment.LOCAL,
                ),
            )
            is None
        )

    def test_enabling_without_an_endpoint_is_refused(self) -> None:
        # A trap worth being loud about: several tracing backends take OTLP
        # traces and nothing else, and metrics sent there vanish silently.
        with pytest.raises(ValidationError, match="nowhere to send"):
            MetricsSettings(enabled=True)


class TestTokensAndDuration:
    async def test_a_call_records_input_and_output_separately(self, recorded: Recorded) -> None:
        with measured(Operation.CHAT, Provider.OPENAI, "gpt-4o-mini") as measurement:
            measurement.record_tokens(input_tokens=120, output_tokens=30)
        by_type = {
            dict(point.attributes or {})[TOKEN_TYPE]: point.sum
            for point in recorded.points(TOKEN_USAGE)
        }
        assert by_type == {"input": 120, "output": 30}

    async def test_the_duration_is_recorded_even_when_the_call_fails(
        self, recorded: Recorded
    ) -> None:
        # A provider timing out at thirty seconds on every call would otherwise
        # show as no latency at all, because none of its calls finished — the
        # opposite of what an operator needs to see.
        with pytest.raises(TimeoutError), measured(Operation.CHAT, Provider.OPENAI, "m"):
            raise TimeoutError
        points = recorded.points(OPERATION_DURATION)
        assert points
        assert recorded.attributes(OPERATION_DURATION)["error.type"] == "TimeoutError"

    def test_the_token_histogram_uses_the_prescribed_buckets(self, recorded: Recorded) -> None:
        # The SDK's defaults top out at ten thousand, which puts every large
        # prompt in one overflow bucket — and the large ones are the interesting
        # ones.
        with measured(Operation.CHAT, Provider.OPENAI, "m") as measurement:
            measurement.record_tokens(input_tokens=1, output_tokens=1)
        point = recorded.points(TOKEN_USAGE)[0]
        assert tuple(point.explicit_bounds) == TOKEN_BUCKETS


class TestCost:
    def test_a_priced_model_produces_an_estimate(self, recorded: Recorded) -> None:
        with measured(Operation.CHAT, Provider.OPENAI, "gpt-4o-mini") as measurement:
            measurement.record_tokens(
                input_tokens=1_000_000, output_tokens=1_000_000, pricing=PRICES
            )
        point = recorded.points(ESTIMATED_COST)[0]
        assert point.sum == pytest.approx(0.75)

    def test_the_estimate_carries_the_price_list_that_produced_it(self, recorded: Recorded) -> None:
        # Without the revision, last month's figures are uninterpretable the
        # moment the table changes — and the table changes.
        with measured(Operation.CHAT, Provider.OPENAI, "gpt-4o-mini") as measurement:
            measurement.record_tokens(input_tokens=10, output_tokens=10, pricing=PRICES)
        assert recorded.attributes(ESTIMATED_COST)[PRICE_REVISION] == "2026-09-01"

    def test_an_unpriced_model_produces_silence_not_zero(self, recorded: Recorded) -> None:
        # Zero is a claim about what something cost. Silence is the truth.
        with measured(Operation.CHAT, Provider.OPENAI, "some-local-model") as measurement:
            measurement.record_tokens(input_tokens=10, output_tokens=10, pricing=PRICES)
        assert recorded.points(ESTIMATED_COST) == []

    def test_no_price_list_means_no_cost_series_at_all(self, recorded: Recorded) -> None:
        with measured(Operation.CHAT, Provider.OPENAI, "gpt-4o-mini") as measurement:
            measurement.record_tokens(input_tokens=10, output_tokens=10)
        assert ESTIMATED_COST not in recorded.names()

    def test_a_price_list_must_say_which_one_it_is(self) -> None:
        with pytest.raises(ValidationError, match="revision"):
            MetricsSettings(
                pricing=PricingSettings(models={"gpt-4o-mini": ModelPrice(input=1, output=2)})
            )


class TestAgentRuns:
    async def test_a_run_records_its_duration(self, recorded: Recorded) -> None:
        with measured_run("incident-triage"):
            pass
        assert recorded.points(AGENT_DURATION)

    async def test_a_run_counts_the_model_calls_made_inside_it(self, recorded: Recorded) -> None:
        with measured_run("incident-triage"):
            for _ in range(3):
                with measured(Operation.CHAT, Provider.OPENAI, "m") as measurement:
                    measurement.record_tokens(input_tokens=1, output_tokens=1)
        assert recorded.points(AGENT_INFERENCE_CALLS)[0].sum == 3

    async def test_calls_from_a_concurrent_branch_are_counted(self, recorded: Recorded) -> None:
        # The reason the counter is a mutable object in a context variable rather
        # than an integer: the orchestrator runs nodes in separate tasks, and
        # asyncio copies the context into each. A child that rebound the variable
        # would count into its own copy and the run would report zero.
        async def branch() -> None:
            with measured(Operation.CHAT, Provider.OPENAI, "m") as measurement:
                measurement.record_tokens(input_tokens=1, output_tokens=1)

        with measured_run("incident-triage"):
            await asyncio.gather(branch(), branch())
        assert recorded.points(AGENT_INFERENCE_CALLS)[0].sum == 2

    async def test_a_run_that_fails_is_still_timed(self, recorded: Recorded) -> None:
        # A dashboard counting only completed runs reports an agent as fast
        # because its slow ones never finish.
        with pytest.raises(RuntimeError), measured_run("incident-triage"):
            raise RuntimeError
        assert recorded.points(AGENT_DURATION)


class TestToolExecutions:
    def test_a_tool_records_its_duration(self, recorded: Recorded) -> None:
        with measured_tool("search_corpus"):
            pass
        assert recorded.points(TOOL_DURATION)

    def test_a_failing_tool_is_timed_and_labelled(self, recorded: Recorded) -> None:
        with pytest.raises(ValueError, match="tool failed"), measured_tool("search_corpus"):
            raise ValueError("tool failed")
        assert recorded.attributes(TOOL_DURATION)["error.type"] == "ValueError"
