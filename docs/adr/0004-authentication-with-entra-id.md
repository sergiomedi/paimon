# ADR-0004: Authentication with Microsoft Entra ID

- **Status:** Accepted
- **Date:** 2026-09-01
- **Phase:** 1 — Foundation

## Context and problem statement

The platform serves multiple concurrent users inside an organization and exposes
operational knowledge that is not uniformly public. Authentication is therefore a Phase 1
deliverable, not a later concern: retrofitting an identity model after use cases, agent
state and audit trails exist is one of the more expensive refactors a system can undergo.

The question is not merely *how users log in*, but **what the platform is responsible for**.
Owning credentials means owning password storage, reset flows, lockout policy, multi-factor
enrolment, session revocation and the breach surface that comes with all of it.

## Decision drivers

- The deployment target is Azure; organizations deploying there already have a directory.
- Credential storage is a liability with no upside for this platform.
- The identity provider must be substitutable — the domain must not know it exists.
- Local development must not depend on a cloud tenant being reachable.

## Considered options

1. **Microsoft Entra ID via OpenID Connect**; the backend validates JWTs against the
   tenant's JWKS endpoint and never sees a credential.
2. **Self-issued JWTs** with local password storage (`fastapi-users` or a custom
   implementation).
3. **A third-party identity SaaS** — Clerk, Auth0, Supabase Auth.

## Decision

Option 1, behind an `IdentityProvider` port.

The backend performs stateless token validation: signature against the cached JWKS,
issuer, audience, expiry, and the claims it maps to a domain `Principal`. It stores no
passwords and issues no tokens.

A second adapter, `DevIdentityProvider`, signs tokens with a locally generated key and is
enabled only when the environment is `local` or `test`. It exists so that development and
integration tests never require tenant connectivity — and it is guarded by a startup check
that refuses to boot if it is selected outside those environments. An authentication bypass
that can be enabled by a stray environment variable is a vulnerability, not a convenience.

Authorization is deliberately kept separate from authentication: the token establishes
*who*, and a domain-level policy decides *what*. Role claims are mapped at the boundary
into domain concepts, so the authorization model does not inherit the shape of whatever
directory happens to be in front of it.

## Consequences

### Positive

- No credential storage: the largest category of authentication vulnerability is removed
  from the platform's attack surface entirely.
- Single sign-on, conditional access and multi-factor authentication come from the
  directory, at no implementation cost.
- Matches how identity actually works in the enterprise environments this platform targets.
- Token validation is stateless, so the API scales horizontally without a session store.

### Negative

- Requires an Entra ID tenant to run against reality; the dev adapter covers the gap but
  does not exercise the real token shape. Mitigated by an integration test suite that runs
  against the tenant in CI on a schedule rather than on every commit.
- JWKS fetching introduces an external dependency in the request path. Mitigated by caching
  keys with a refresh on unknown key id, and failing closed.
- Tenant configuration (app registration, scopes, audience) is setup work with no visible
  product output, and must be documented for anyone cloning the repository.

### Neutral

- Multi-tenancy at the application level is a separate concern from directory tenancy. The
  domain carries its own `tenant_id`; the mapping from directory claims is explicit.

## Alternatives in detail

### Option 2 — Self-issued JWTs with local passwords

The conventional tutorial answer, and the reason so many portfolio projects contain a
hand-rolled password reset flow. It would demonstrate competence in a problem this project
is not about, while adding breach liability, and it does not reflect how access is managed
in the organizations the platform is designed for. It would become the right choice for a
consumer-facing product with no corporate directory behind it.

### Option 3 — Identity SaaS (Clerk, Auth0)

Excellent developer experience and the fastest path to a working login screen. Rejected on
two grounds: it introduces a vendor outside the Azure stack for a problem the Azure stack
already solves, and — for a portfolio judged on architectural judgement — outsourcing
identity to a drop-in widget demonstrates less than integrating with an enterprise
directory. Note that Option 1 and Option 3 cost roughly the same to implement behind the
port, which is precisely the point of the port.
