# ADR-0041: Telemetry leaves through a collector we control

- **Status:** Accepted
- **Date:** 2026-09-11
- **Phase:** 7 — Cloud

## Context and problem statement

ADR-0025 decided that the application emits plain OpenTelemetry over OTLP and that
a backend is an endpoint and a credential rather than a dependency. It also named
the moment this would be tested: *"Azure Monitor in Phase 7, when there is an Azure
deployment to attach it to."* This is that moment, and Azure Monitor turns out to
be the one backend that does not simply accept OTLP.

Azure Container Apps offers a managed OpenTelemetry agent that would make this an
empty decision — a few lines on the environment resource and no collector to run.
It is the obvious answer, and it does not fit.

## Decision drivers

- No vendor SDK in the application. That is ADR-0025 and this is its first real
  test.
- The platform emits **metrics as well as traces**, and its metrics are the
  interesting half: tokens, cost, retrieval quality.
- The application exports OTLP over **HTTP**, which is what its dependencies are.
- A telemetry backend that stops answering must not become this platform's
  outage.
- Every other service in this environment had its key path switched off
  (ADR-0037, ADR-0038).

## Considered options

1. **The Azure Monitor OpenTelemetry Distro** in the application. The documented,
   supported path — and a vendor SDK in the process, which is exactly what
   ADR-0025 declined.
2. **The Container Apps managed agent.** No collector to run, no image to pin, no
   configuration file. Four problems, and the first is fatal:
   - **It cannot send metrics to Application Insights.** Microsoft's own
     destination matrix says so: logs yes, traces yes, metrics no. Metrics go to
     Datadog, Dynatrace, New Relic, Elastic or a custom OTLP endpoint — every
     destination except the Azure one.
   - It accepts OTLP over **gRPC only**, and this platform's exporters are the
     HTTP ones. That is a dependency change to accommodate an agent.
   - It is configured on the **environment**, not the application, so the two
     apps here cannot be routed differently.
   - It requires Application Insights to **allow local authentication**, and it
     is in preview.
3. **An OpenTelemetry collector we run**, as a second container app.

## Decision

Option 3. A plain upstream `opentelemetry-collector-contrib` image, pinned, with
internal-only ingress on 4318, exporting to Application Insights.

Nothing about the application changes. It exports OTLP/HTTP to an endpoint, which
is what it already did; the endpoint is now a hostname inside the environment
rather than a public one. The collector is where Azure-specific knowledge lives,
and it is a configuration file rather than a library — which is the whole shape
ADR-0025 was arguing for, made concrete.

**Keyless, like everything else.** The component is created with
`DisableLocalAuth: true` and the collector authenticates with the same
user-assigned identity everything else uses, through the collector's Azure
authentication extension, holding the `Monitoring Metrics Publisher` role on the
component. The connection string is still in the collector's configuration,
because the exporter needs it to know *which* resource to write to — but with
local authentication off it names a destination rather than authorizing a write.
That distinction is ADR-0037's argument exactly, and it is why an instrumentation
key being "not a secret" is beside the point: anything holding one could write
telemetry into a workspace this project is billed for.

**The configuration is a file, not a string.** `infrastructure/collector.yaml`,
loaded at compile time with `loadTextContent()` and finished with two
substitutions. It is a file so that an editor lints it and `check.sh` parses it —
Bicep's `loadTextContent()` verifies that a file exists and nothing whatsoever
about what is in it, and a template that compiles cleanly around malformed YAML
is a deployment that succeeds and a collector that crash-loops. The check is the
same shape as the one added for `outputs.py` in batch 1b, for the same reason and
after the same lesson.

**One replica, not zero.** The opposite of the choice made for the API, and
deliberately: a collector asleep when telemetry arrives loses the spans of a cold
start, which are the ones worth having, and one scaled to zero mid-flush loses
whatever was in the batch. About 0.01 EUR an hour buys not having that argument.

## Consequences

**Positive.** The application is unchanged and remains portable: Langfuse,
Azure Monitor, or both at once, is a pipeline in a YAML file nobody has to
redeploy code for. Metrics work, which they would not have under the managed
agent. Application Insights becomes the fourth service in the environment with
its key path switched off. And the collector absorbs backend trouble — a backend
that stops answering fills a queue in the collector rather than blocking the
application.

**Negative — it is a container to run, patch and pay for.** Small, but real: an
image to pin, a version to bump, a process that can itself fail. The managed agent
exists precisely to remove that, and on a deployment that needed only traces it
would be the right answer.

**Negative — the exporter and the authentication extension are both beta.** The
Azure Monitor exporter is beta for all three signals; the authentication extension
is beta and had a server-side authentication bypass in versions 0.124.0 through
0.150.0. Only its client side is used here and the pin is past the fix, but "beta"
is the honest description of this path, and the alternative that is not beta is a
vendor SDK in the application.

**Negative — nothing reports that the export is failing.** The collector's health
endpoint says it is accepting data, not that it is managing to deliver it. An
exporter that cannot authenticate keeps the collector healthy and drops
everything, and the only place it shows up is the collector's own log stream. That
is a real gap; closing it means scraping the collector's internal metrics, which
is worth doing when something depends on this telemetry rather than measures with
it.

**Logs do not go through it.** Structured JSON on stdout already reaches the same
workspace through Container Apps' own log shipping. Routing them through the
collector would produce two copies of every line and bill for both.
