"""Measurements, as distinct from traces.

A trace answers *what happened in this request*. A metric answers *what has been
happening*, and the two are not substitutes: searching a month of spans to total
a bill is an expensive way to compute a sum that could have been a counter, and
a metric can never tell you why one particular run was slow.

So this batch adds the aggregation half — tokens, durations, and an **estimated**
cost — from the same decorators that already record the spans. The numbers are
the ones already in hand, which is why they cost nothing new to produce.

**Cost is not measured, and the conventions do not pretend otherwise.** There is
no ``gen_ai`` cost attribute or metric anywhere in the specification: the
ecosystem's position is that cost is derived downstream from tokens and a price
list. This platform derives it too, and says so in the metric's own name — it is
in the ``paimon.`` namespace, it carries the revision of the table that produced
it, and a model with no price produces no measurement rather than a zero.

**Metrics do not necessarily go where traces go.** Several tracing backends,
Langfuse among them, accept OTLP traces and nothing else. The endpoint is
configured separately for that reason (ADR-0028).
"""

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource

from paimon.config import MetricsSettings

#: The two client metrics the conventions define for a model call.
TOKEN_USAGE = "gen_ai.client.token.usage"  # noqa: S105  a metric name, not a credential
OPERATION_DURATION = "gen_ai.client.operation.duration"

#: Agent runs. Durations in the same units, so one dashboard can hold both.
AGENT_DURATION = "gen_ai.invoke_agent.duration"
AGENT_INFERENCE_CALLS = "gen_ai.invoke_agent.inference_calls"
TOOL_DURATION = "gen_ai.execute_tool.duration"

#: Ours, and named so it is obvious: this is arithmetic over a table somebody
#: typed, not something the provider reported.
ESTIMATED_COST = "paimon.gen_ai.cost.estimated"

TOKEN_TYPE = "gen_ai.token.type"  # noqa: S105  an attribute name, not a credential
CURRENCY = "paimon.pricing.currency"
PRICE_REVISION = "paimon.pricing.revision"

#: Prescribed by the conventions. Worth setting explicitly rather than taking the
#: SDK's defaults, whose largest bucket is ten thousand — every prompt above that
#: would land in the same overflow bucket, which is where the interesting ones
#: are.
TOKEN_BUCKETS = (
    1.0,
    4.0,
    16.0,
    64.0,
    256.0,
    1024.0,
    4096.0,
    16384.0,
    65536.0,
    262144.0,
    1048576.0,
    4194304.0,
    16777216.0,
    67108864.0,
)
DURATION_BUCKETS = (
    0.01,
    0.02,
    0.04,
    0.08,
    0.16,
    0.32,
    0.64,
    1.28,
    2.56,
    5.12,
    10.24,
    20.48,
    40.96,
    81.92,
)
AGENT_DURATION_BUCKETS = (
    0.1,
    0.2,
    0.4,
    0.8,
    1.6,
    3.2,
    6.4,
    12.8,
    25.6,
    51.2,
    102.4,
    204.8,
    409.6,
)
CALL_COUNT_BUCKETS = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0)

INSTRUMENTATION_SCOPE = "paimon"


def build_meter_provider(settings: MetricsSettings, *, resource: Resource) -> MeterProvider | None:
    """Build the provider for this process, or None when metrics are off.

    Args:
        settings: Whether to export, where to, and how often.
        resource: The same resource the traces carry, so a backend can line the
            two up against one service rather than two.

    Returns:
        A configured provider, or None if metrics are disabled.
    """
    if not settings.enabled or not settings.endpoint:
        return None
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=settings.endpoint, headers=dict(settings.headers)),
        export_interval_millis=int(settings.export_interval_seconds * 1000),
    )
    return MeterProvider(resource=resource, metric_readers=[reader], views=_views())


def _views() -> list[View]:
    """Give each histogram the buckets its measurements actually occupy.

    Without these every histogram gets the SDK's default boundaries, which top
    out at ten thousand. For durations in seconds that is one bucket nothing ever
    leaves; for token counts it puts every large prompt in the same overflow.
    Either way the percentiles a dashboard shows are fiction.
    """
    return [
        View(instrument_name=TOKEN_USAGE, aggregation=_buckets(TOKEN_BUCKETS)),
        View(instrument_name=OPERATION_DURATION, aggregation=_buckets(DURATION_BUCKETS)),
        View(instrument_name=TOOL_DURATION, aggregation=_buckets(DURATION_BUCKETS)),
        View(instrument_name=AGENT_DURATION, aggregation=_buckets(AGENT_DURATION_BUCKETS)),
        View(instrument_name=AGENT_INFERENCE_CALLS, aggregation=_buckets(CALL_COUNT_BUCKETS)),
    ]


def _buckets(boundaries: tuple[float, ...]) -> ExplicitBucketHistogramAggregation:
    return ExplicitBucketHistogramAggregation(boundaries=boundaries)


def install(provider: MeterProvider | None) -> None:
    """Make a provider the process-wide default.

    None leaves the API's no-op in place, which is what "metrics are off" means:
    every ``record`` call elsewhere still runs and costs almost nothing.
    """
    if provider is not None:
        metrics.set_meter_provider(provider)


def shutdown(provider: MeterProvider | None) -> None:
    """Flush and stop a provider at shutdown.

    Metrics are exported on an interval, so without this the measurements since
    the last push die with the process — and a process that is shutting down
    unexpectedly is exactly when that interval's numbers matter.
    """
    if provider is not None:
        provider.shutdown()


def get_meter() -> metrics.Meter:
    """Return this platform's meter.

    Resolved per call rather than captured at import, for the same reason the
    tracer is: the provider is installed during startup, and an instrument
    created at import would be bound to the no-op that preceded it.
    """
    return metrics.get_meter(INSTRUMENTATION_SCOPE)


__all__ = [
    "AGENT_DURATION",
    "AGENT_DURATION_BUCKETS",
    "AGENT_INFERENCE_CALLS",
    "CALL_COUNT_BUCKETS",
    "CURRENCY",
    "DURATION_BUCKETS",
    "ESTIMATED_COST",
    "INSTRUMENTATION_SCOPE",
    "OPERATION_DURATION",
    "PRICE_REVISION",
    "TOKEN_BUCKETS",
    "TOKEN_TYPE",
    "TOKEN_USAGE",
    "TOOL_DURATION",
    "build_meter_provider",
    "get_meter",
    "install",
    "shutdown",
]
