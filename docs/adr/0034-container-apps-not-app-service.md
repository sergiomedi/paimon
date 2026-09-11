# ADR-0034: The platform runs on Container Apps, not App Service

- **Status:** Accepted
- **Date:** 2026-09-11
- **Phase:** 7 — Cloud

## Context and problem statement

The phase brief names both Azure App Service and Azure Container Apps. Only one of them
gets built, and Microsoft's own comparison does not settle it. Its guidance says App
Service "is optimized for web applications" and that "when building web apps, Azure App
Service is an ideal option", while positioning Container Apps as the managed
Kubernetes-shaped option for teams that do not need the Kubernetes API. A FastAPI backend
serving HTTP is, on that description, an App Service workload.

So the argument has to come from this system rather than from a comparison table.

## Decision drivers

- A container image already exists and is built and smoke-tested in CI from Phase 1.
- This environment will hold **more than one workload**: an OpenTelemetry collector, and
  in Phase 8 a migration job that runs to completion and exits.
- The budget for this phase is a fixed amount of trial credit, so anything that bills
  while nothing is happening is a real constraint rather than a rounding error.
- PostgreSQL will want a private endpoint, and the compute has to be able to reach it.

## Considered options

1. **App Service (Web App for Containers).** Simplest for one HTTP application. Always-on
   by default; scaling to zero is not part of the model. A second workload means a second
   plan or a second app on the same plan, and a job that runs and exits is a WebJob,
   which is a different execution model with different tooling.
2. **Azure Kubernetes Service.** Everything, at the cost of owning a cluster. Nothing in
   this system needs the Kubernetes API, and the operational surface is the whole point
   of not choosing it.
3. **Azure Container Apps.** One environment hosting several containerized workloads,
   KEDA-based scaling including to zero, jobs as a first-class resource, and revisions
   with traffic weights.

## Decision

Container Apps, on a **workload-profiles environment** running the **Consumption** profile.

The deciding argument is that this is not one HTTP application. It is an API, a telemetry
collector that must sit beside it, and — one phase from now — a migration that runs before
a release and exits. Container Apps hosts those three as one environment with one identity,
one log destination and one network boundary. App Service hosts the first one well and the
other two by working around it.

Scale to zero matters here for a reason that will not be true of a real production
deployment and is true of this one: the environment exists to be measured and then
destroyed, and between measurements it should cost nothing.

**Workload profiles rather than a Consumption-only environment**, even though every
workload runs on the Consumption profile. The Consumption-only environment is the legacy
shape: it cannot take a custom VNet subnet with user-defined routes, cannot take private
endpoints, and cannot be converted afterwards. Both cost the same while nothing is running.
Choosing the one that can grow is free; choosing the one that cannot is a migration.

## Consequences

**Positive.** The collector in ADR-0037 is a second container app rather than a second
piece of infrastructure. Revisions give zero-downtime deployment without anything being
configured for it: in the default single-revision mode the old revision keeps all traffic
until the new one is provisioned, scaled to the previous replica count, and passing its
probes. Rollback is re-pointing traffic at a revision that is still there.

**Negative, and specific.**

- **Ingress has a request timeout**, 240 seconds by default. This platform streams agent
  runs, and an agent run that streams for longer than that is cut off by the platform it
  runs on rather than by anything in the code. Premium ingress raises the ceiling; the
  cheaper answer is that a run which takes four minutes should be a suspended run
  (ADR-0019) rather than a long request, which is the design already.
- **Probes are HTTP or TCP only.** No `exec`, no gRPC. That is fine here — the health
  endpoints are HTTP already — but it removes an option some deployments rely on.
- **Cold start is real.** With `minReplicas: 0`, the first request after an idle period
  pays image pull plus interpreter start plus whatever the application does before it
  serves. That is acceptable for an environment measured deliberately and would not be for
  a user-facing service, which is why the replica floor is a parameter and not a constant.
- **No Kubernetes API.** If this platform ever needs one, this is the decision that has to
  be revisited, and the container image is what makes revisiting it cheap.

**What this does not decide.** How the app is configured, scaled and probed is
ADR-0038's, alongside the code changes deployment forces. This ADR places the workload; it
does not describe it.
