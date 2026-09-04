# Observing Paimon

The platform emits **plain OpenTelemetry**: traces, and measurements. There is no vendor SDK
anywhere in the code, so the backend is an endpoint and a credential rather than a dependency
([ADR-0025](adr/0025-opentelemetry-as-the-only-instrumentation.md)).

Everything here is **off by default**. A deployment with nowhere to send telemetry pays nothing
for it, and enabling either half without an endpoint is refused at startup — a process
collecting into nowhere looks exactly like one that works, right up until somebody opens the
backend and finds it empty.

---

## What gets emitted, and what each answers

| Signal | Question it answers |
|---|---|
| **Logs** | What happened, in words. Every line carries a correlation id, the trace and the span it was written inside. |
| **Traces** | Where the time went in *this* request, and what depended on what. |
| **Metrics** | What has been happening. Totals, percentiles, cost — questions no search over spans should have to answer. |

They join in both directions: a log line names its trace, and a request's span carries the
correlation id the logs are keyed by. So a reader who starts from an error message reaches the
timing, and one who starts from a slow span reaches the words.

### The spans

| Span | When | Notable attributes |
|---|---|---|
| `GET /api/v1/...` | Every request, from the ASGI instrumentation | `http.route`, `paimon.correlation_id` |
| `chat {model}` | A generation call | `gen_ai.operation.name`, `gen_ai.provider.name`, requested and returned model, token usage |
| `embeddings {model}` | An embedding call | input count, embeddings returned |
| `retrieval {index}` | A search | `gen_ai.data_source.id`, `gen_ai.retrieval.top_k`, hits returned, which strategy ran |
| `invoke_agent {agent}` | A whole agent run, with its nodes nested inside | `gen_ai.agent.name`, `gen_ai.conversation.id` (the thread id) |
| `invoke_agent {agent}.{node}` | Each node of a graph | `paimon.agent.node`, tokens the step spent |
| `execute_tool {tool}` | A tool call arriving over MCP | `gen_ai.tool.name`, call id, **tenant** |
| SQL statements | Every database call | from the SQLAlchemy instrumentation |
| Outgoing HTTP | Every provider call, **including retries** | from the httpx instrumentation |

The nesting is the point. A model call's span covers the whole logical call and the HTTP
attempts sit inside it, so *"the model took nine seconds"* and *"the model took three attempts"*
are visible at once. Agent nodes nest inside their run, so nothing has to be reassembled by
timestamp.

Health probes are excluded. A readiness check every few seconds outnumbers the traffic it
watches, and paying to store those buys a dashboard whose busiest endpoint is the one nobody
uses.

### The metrics

| Metric | Unit | What it is for |
|---|---|---|
| `gen_ai.client.token.usage` | `{token}` | Tokens, split by `gen_ai.token.type` into input and output |
| `gen_ai.client.operation.duration` | `s` | Model call latency, **including calls that failed**, tagged with `error.type` |
| `gen_ai.invoke_agent.duration` | `s` | How long a run took, whether it succeeded, failed or stopped for a person |
| `gen_ai.invoke_agent.inference_calls` | `{inference_call}` | Exactly how many model calls one run made |
| `gen_ai.execute_tool.duration` | `s` | Tool execution latency |
| `paimon.gen_ai.cost.estimated` | — | See below. Not measured. |

Histogram buckets are set to the boundaries the conventions prescribe rather than the SDK's
defaults, which top out at ten thousand — a bucket durations never leave, and one that puts
every large prompt in the same overflow.

---

## Cost: what this number is, and what it is not

There is **no cost attribute or metric in the OpenTelemetry conventions**. The ecosystem's
position is that cost is derived downstream from token counts and a price list, and this
platform derives it too — visibly ([ADR-0028](adr/0028-metrics-and-an-estimated-cost.md)).

`paimon.gen_ai.cost.estimated` is in this platform's own namespace with the word *estimated* in
the name, because that is what it is: arithmetic over a table somebody typed. **The provider's
invoice is the authority.** What this buys is the shape of a bill before it arrives — which
tenant, which model, which day.

Three properties keep it honest:

- A model absent from the price list produces **no measurement**, not a zero. Zero is a claim
  about what something cost; silence is the truth.
- Every measurement carries the price list's **revision**, and prices without one are refused at
  startup. A figure that cannot be traced back to the prices behind it stops meaning anything
  the moment the table changes — and the table changes.
- The **currency** is on the measurement, because a chart mixing two is worse than no chart.

---

## Connecting a backend

### Langfuse

Langfuse accepts OTLP and reads the `gen_ai.*` conventions directly, so no code changes and no
SDK. Build the credential from your project keys:

```bash
printf '%s' "pk-lf-...:sk-lf-..." | base64 -w 0
```

```bash
PAIMON_OBSERVABILITY__TRACING__ENABLED=true
PAIMON_OBSERVABILITY__TRACING__ENDPOINT=https://cloud.langfuse.com/api/public/otel/v1/traces
PAIMON_OBSERVABILITY__TRACING__HEADERS='{"Authorization":"Basic <base64>","x-langfuse-ingestion-version":"4"}'
```

> **Metrics do not go here.** Langfuse accepts OTLP **traces** and nothing else. Metrics pointed
> at a traces endpoint fail quietly, and the symptom is an empty dashboard that looks like a
> platform emitting nothing. Send them to a collector, or to a backend that takes them.

### A collector, for both

```bash
PAIMON_OBSERVABILITY__TRACING__ENABLED=true
PAIMON_OBSERVABILITY__TRACING__ENDPOINT=http://localhost:4318/v1/traces
PAIMON_OBSERVABILITY__METRICS__ENABLED=true
PAIMON_OBSERVABILITY__METRICS__ENDPOINT=http://localhost:4318/v1/metrics
```

Both providers share one resource — `service.name`, `service.version`, `deployment.environment`
— so a backend receiving both reads them as one service rather than two with similar names.

### Prices, if you want a cost figure

```bash
PAIMON_OBSERVABILITY__METRICS__PRICING__CURRENCY=USD
PAIMON_OBSERVABILITY__METRICS__PRICING__REVISION=2026-09-01
PAIMON_OBSERVABILITY__METRICS__PRICING__MODELS='{"gpt-4o-mini":{"input":0.15,"output":0.60}}'
```

Per **million** tokens, because that is the unit providers publish. Converting at the point of
configuration is where a factor of a thousand goes unnoticed.

### Sampling

```bash
PAIMON_OBSERVABILITY__TRACING__SAMPLE_RATIO=0.1
```

Head-based and parent-respecting: the decision is made once at the root of a trace and
inherited, so a kept trace is **complete**. A service that re-decided halfway through would
produce traces with holes in them, which are harder to read than traces that were never kept.

---

## Prompts and completions

**Off by default, and refused outright in staging and production.**

The conventions mark message content opt-in and warn that it is likely to carry sensitive data.
Here that content is an organization's internal documentation and whatever its people typed into
a box. Turning that export on where real tenants' material flows should cost a code change and a
review, and this guard is what makes it cost one — the same shape as the guard that already
refuses SQL echo outside local environments.

```bash
# Local and test only. Rejected at startup anywhere else.
PAIMON_OBSERVABILITY__TRACING__CAPTURE_CONTENT=true
```

What is **never** recorded, switch or no switch:

- **Tool arguments.** They are the caller's data. Only the names of the tools a model chose,
  which is the shape of its reasoning.
- **What is embedded.** During ingestion that is the corpus itself, so recording it would copy
  an organization's documentation into a tracing backend one chunk at a time. The embedding
  wrapper has no content switch at all.

---

## Where the instrumentation lives

Nothing above infrastructure is instrumented, and that is a decision rather than an oversight:
**what is worth tracing is what crosses a port**. A model call, a vector search, an HTTP request,
a node in a graph. The domain performs no I/O, so it has nothing to trace, and a `Tracer`
threaded through use cases would be an abstraction over an API that is already one — paid for in
every signature it passed through.

Model calls, embeddings and retrieval are traced by **wrapping the port**, not by editing the
adapters ([ADR-0026](adr/0026-tracing-by-decoration.md)). There are two adapters of each and
there will be more; instrumentation written into every one makes tracing an author's
responsibility, and the failure mode of forgetting is a gap in a dashboard nobody notices,
because nothing broke.

One hazard came with that, twice. `ToolCallingChatModel` and `NativeHybridSearch` are
capabilities this platform checks with `isinstance`. A wrapper implementing only the base port
would silently remove them — agents quietly stop being offered tools; Azure AI Search quietly
stops using its own fusion. Neither raises. Every wrapper picks its shape from what it is given,
and the first thing a wrapper's tests assert is that the capability survived.

---

## Known gaps

- **The newest spans are the likeliest to be lost.** Exporting is batched, so a process that
  dies badly takes the batch describing how it died. The shutdown path flushes; a hard kill does
  not get to.
- **A price list goes stale silently.** The revision attribute makes staleness visible *in the
  data* rather than preventing it, which is the most a platform can do about a number it does
  not own.
- **Span volume grows with graph size.** A six-node run is eight spans before the model and
  retrieval calls beneath it. Sampling is the control.
- **The conventions are still moving.** No `gen_ai` span, metric or attribute is marked Stable;
  two have already been renamed. Every name this platform writes is a constant in one module, so
  the next rename is one file.
