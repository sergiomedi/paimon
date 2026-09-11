# ADR-0038: A database with no password and no public address

- **Status:** Accepted
- **Date:** 2026-09-11
- **Phase:** 7 — Cloud

## Context and problem statement

ADR-0037 disabled key authentication on the model and search services, on the grounds that
leaving an unused credential enabled makes the claim "this platform stores no credentials"
unfalsifiable. The database is the same argument with higher stakes: it is the only resource
in this environment that holds anything, and a leaked database credential is not a bill, it
is a breach.

It is also the resource where the usual answer — a password in Key Vault — is most
comfortable and most defensible. So the question is whether removing the password entirely
is worth what it costs.

## Decision drivers

- The database holds documents, agent state and vectors. Everything else is derivable.
- A credential that exists has to be rotated, and rotation is the operation nobody does.
- The workload's identity already exists and already authenticates to three other services.
- A deployment that is created and destroyed repeatedly must not require a manual secret
  step in between.

## Considered options

1. **A generated password in Key Vault**, read by the app through its managed identity.
   Fewer moving parts, and the secret's blast radius is small. But it is a secret: it exists
   in the vault, in the deployment output that put it there, and in whatever created it.
2. **Microsoft Entra authentication, password authentication left enabled.** Best of both,
   in the sense that the weaker path is still open and nothing tells you when it is used.
3. **Entra only: `passwordAuth: 'Disabled'` on the server.**

## Decision

Option 3, with the server on a private endpoint and no public network access.

```bicep
authConfig: {
  activeDirectoryAuth: 'Enabled'
  passwordAuth: 'Disabled'
  tenantId: subscription().tenantId
}
network: {
  publicNetworkAccess: 'Disabled'
}
```

There is no `administratorLogin` and no `administratorLoginPassword` in the template. Their
absence is the feature.

**The interesting part is not the configuration, it is what it does to the connection pool.**
Under Entra the access token is presented *in the password field*, and it is validated
**when a connection opens and never again**. That produces a failure with an unusually nasty
shape: a pool whose existing connections keep working indefinitely, while the next
connection it decides to open cannot authenticate — an hour after a deployment, on a code
path nobody touched, under load rather than in testing.

So the token is fetched **per physical connection**, in SQLAlchemy's `do_connect` hook,
rather than built into a connection string once at startup. A connection string is exactly
the wrong place for a credential with an expiry. Connections are also recycled at half an
hour, well inside a token's life, so the pool never holds one much older than the token that
opened it.

That behaviour is pinned by a test that asserts the second connection receives a *different*
token from the first, because a token fetched once and cached forever passes every other
test in the suite.

**No public address.** The server is not reachable from the internet at all — not "public
with an empty firewall list", which is one accidental rule away from being reachable. A
private endpoint in the subnet the application runs in is the only route, which is why this
batch also creates the virtual network, and why the Container Apps environment was built
with VNet integration from the start: that setting is fixed when the environment is created
(ADR-0034), so adding it when the database arrived would have meant rebuilding the
environment. On an ephemeral deployment that is two commands; on a permanent one it is an
outage.

## Consequences

**Positive.** There is no database credential anywhere: not in configuration, not in the
vault, not in a deployment output, not in anyone's shell history. The database cannot be
reached from the internet at all. And an environment can be created and destroyed without a
secret-shaped step in between, which is what keeps the teardown honest.

**Negative — a manual bootstrap remains.** A managed identity cannot log in until a role
exists for it inside PostgreSQL, and creating that role is SQL, not ARM:

```sql
SELECT * FROM pgaadauth_create_principal('id-paimon-dev', false, false);
```

It is run once per environment by the Entra administrator, from inside the network. That is
a real step in a process otherwise reduced to one command, and it is documented in the
deployment guide rather than discovered.

**Negative — resumable agent runs are refused with Entra.** The graph checkpointer
(ADR-0017) reaches the same database through a second driver and takes a connection string
built once. A token in that string expires while the process keeps running. The combination
is refused at startup with an explanation rather than allowed to fail hours in; supporting
it means giving the checkpointer a per-connection credential of its own, and that is worth
building when resumable runs are actually deployed.

**Negative — everything now needs to be inside the network.** Running the benchmark against
the deployed database from a laptop does not work, and will not, which is the intended
consequence of the previous paragraph about public addresses. The benchmark's hybrid mode —
local PostgreSQL, Azure models and search — exists partly for this reason.

**Cost.** General Purpose rather than Burstable, at roughly 0.25 EUR an hour. Microsoft is
explicit that Burstable is not for production, it supports no high availability and has no
connection pooler, and vector search is precisely the workload that exhausts its CPU
credits. Cheaper would have been available and would have been a demo described as
production. This is the batch that makes `destroy.sh` matter: a month of this server costs
more than the entire budget for the phase.
