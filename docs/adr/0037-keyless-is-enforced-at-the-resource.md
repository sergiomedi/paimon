# ADR-0037: Keyless is enforced at the resource, not chosen by the client

- **Status:** Accepted
- **Date:** 2026-09-11
- **Phase:** 7 — Cloud

## Context and problem statement

ADR-0014 decided how this platform authenticates to Azure: if a key is present in
configuration it is used, and if one is absent the adapter acquires a token through
`DefaultAzureCredential`. That made the keyless path *possible*. It did not make it
*true* — a deployment could still be handed a key, and nothing would notice.

Provisioning the services for real forces the question. Azure OpenAI and Azure AI Search
both issue admin keys at creation, both accept them by default, and on Azure AI Search the
default is stronger than that: **a new service is key-only, and rejects role-based requests
until it is told not to.** Assigning the roles and stopping there produces a service that
refuses every call the platform makes, with a 403 that does not mention the reason.

## Decision drivers

- "This platform stores no credentials" is a claim this repository makes. A claim that
  cannot fail is not a claim.
- The failure mode of a half-configured keyless setup is a 403 with no explanation, which
  is a bad afternoon for whoever hits it.
- The environment is created and destroyed repeatedly, so anything that has to be
  remembered after provisioning will eventually be forgotten.

## Considered options

1. **Keys in Key Vault, read by the app.** Works everywhere, including where managed
   identity is not available. Keeps a secret in existence, to be rotated, leaked or
   committed.
2. **Roles assigned, keys left enabled.** The common shape, and the one most guides
   describe. The platform uses tokens; the keys sit there unused.
3. **Roles assigned, and local authentication disabled at the resource.**

## Decision

Option 3, on both services, in the template.

```bicep
properties: {
  disableLocalAuth: true
}
```

Option 2 is the one worth arguing against, because it looks identical in normal operation.
The difference appears the day something regresses: a stray `PAIMON_AZURE_SEARCH__API_KEY`
in an environment file, a fallback added "temporarily", a copied snippet. Under option 2
that regression **works**, silently, and the platform's claim quietly stops being true.
Under option 3 it fails on the first call. The point of disabling the key path is not that
the keys are dangerous where they sit — it is that leaving them enabled makes the
architecture unfalsifiable.

Three details that are not obvious and cost an afternoon each:

**A custom subdomain is required for token authentication.** Without
`customSubDomainName`, an Azure OpenAI account is reachable only at the shared regional
endpoint, which does not accept Microsoft Entra tokens. The failure is a 401 that says
nothing about subdomains.

**Search rejects role-based access until the service is switched.** Role assignments are
necessary and not sufficient. This is the setting that almost every walkthrough of "use
managed identity with Azure AI Search" omits, including the one previously in this
repository's own `docs/azure-setup.md`, which listed the role assignments and stopped.

**Role assignments take minutes, sometimes longer, to propagate.** A deployment that
finishes and is immediately followed by a 403 is usually not misconfigured. Waiting is the
fix, and knowing that is the difference between waiting and re-provisioning.

## The role model

Two principals, and they are deliberately not given the same thing:

| Principal | Azure OpenAI | Azure AI Search |
|---|---|---|
| The workload's managed identity | Cognitive Services OpenAI User | Search Index Data Contributor |
| The operator | Cognitive Services OpenAI User | Search Index Data Contributor, **Search Service Contributor** |

`Search Service Contributor` creates and changes **index definitions** — and notably
cannot query an index or write a document to one. It is a deploy-time role and the
workload does not have it. The index schema belongs to the application and is created by a
deliberate command, not by whichever process happens to start first; a workload that could
rewrite the schema it depends on can destroy a corpus by restarting with a changed
constant.

The operator has the data-plane roles for a reason that will be obvious in an hour: the
first thing to touch these services is the evaluation benchmark, run from a laptop.

## Consequences

**Positive.** There is no secret to rotate, leak or commit for either service. The keyless
claim is now checkable: enable a key in configuration and the platform stops working.

**Negative.** Anything that genuinely needs key authentication — a tool, a script, a
support engineer's `curl` — does not work against these resources without changing the
template first. That is the intended cost, and it is a real one.

**Negative.** Every caller now needs a role assignment, including humans. A new person
looking at the search index in the portal sees nothing until somebody grants them a
data-plane role, and the portal's message for this is not helpful.

**Not done here.** Public network access is left enabled on both. Restricting it means a
private endpoint and a VNet, which is the next batch's work and arrives with the database
that actually holds data. Until then these services are reachable from the internet and
protected by Entra alone — which is the honest description, rather than calling
token-authenticated the same thing as network-isolated.
