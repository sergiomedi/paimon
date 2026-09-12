# ADR-0043: A person gets a token the same way a workload does

- **Status:** Accepted
- **Date:** 2026-09-12
- **Phase:** 7 — Cloud

## Context and problem statement

The environment was deployed, the schema was current, the container was running
and the health endpoint answered. It could not be called.

Every authenticated request came back 401, which is the correct answer to a
request with no acceptable token and says nothing about why there was no
acceptable token. There were four candidate reasons at once, and no way to tell
them apart from outside:

- The app registration exposed **no delegated scope**, so no client could ask for
  a token for it at all. `az ad app create` does not create one.
- **Nobody had consented** for the Azure CLI to call this API. Consent is granted
  per client application, and the Azure CLI's registration belongs to Microsoft:
  there is no owner to click through a consent prompt.
- The registration issued **v1.0 tokens**, because `requestedAccessTokenVersion`
  defaults to null and null means v1. This API pins the v2.0 issuer
  (`login.microsoftonline.com/<tenant>/v2.0`), and a v1.0 token is issued by
  `sts.windows.net` — so it fails verification on the *issuer*, which reads like
  a tenant problem rather than a property of the registration.
- The **audience is spelled two ways**, and which one Entra puts in the `aud`
  claim is decided by that same token-version property: a v1.0 token carries the
  application ID URI (`api://<app id>`), a v2.0 token carries the bare
  application id. The audience is baked into the container at deployment time, so
  picking the wrong one is not a setting to correct — it is an API that rejects
  every token until the next deployment.

Phase 7's argument is that a deployment exists to be measured. A deployment that
cannot be called cannot be measured, and this was the last thing standing between
the two.

## Decision drivers

- No secret, anywhere. ADR-0037 made the services keyless; the person calling
  them should not be the exception that reintroduces a credential.
- The failure has to name itself. Four causes that all produce one 401 is the
  shape of problem that costs an afternoon.
- Whatever is done to the app registration must be idempotent and must not
  quietly undo something a future client depends on.
- A token is a credential. Anything that handles one has to assume its output
  will be pasted into a chat window, because that is what happens to the output
  of these scripts.

## Considered options

1. **A client secret on the app registration**, exported and used in a
   client-credentials flow. It works everywhere, it is what most guides show, and
   it puts a long-lived password in somebody's shell history and `.env` file — in
   a phase whose central claim is that nothing here has a password.
2. **A second app registration for the caller**, authorised against the first.
   Correct, and the standard shape for a real client. It is also two objects to
   create, consent to and explain, for a caller that is one person with a
   terminal.
3. **Pre-authorise the Azure CLI against a delegated scope on the API**, and
   acquire tokens with `az account get-access-token`. Pre-authorisation is
   Microsoft's mechanism for exactly this: a resource names a client it trusts,
   and users of that client are never prompted.

## Decision

Option 3, plus accepting both spellings of the audience.

`scripts/azure/token.sh` makes three changes to the app registration, once, and
reports rather than repeats them when they are already made:

- a delegated `user_impersonation` scope, keeping the id of an existing one so
  that consent and pre-authorisation still refer to the same thing;
- the Azure CLI (`04b07795-8ddb-461a-bbee-02f9e1bf7b46`, the same in every
  tenant) pre-authorised for that scope;
- `requestedAccessTokenVersion: 2`, which is what makes the issuer match what the
  API verifies against.

Existing scopes and pre-authorised clients are carried across rather than
replaced, because Microsoft Graph replaces these collections wholesale and a
deployment script that silently removes another client's authorisation is a worse
failure than one that does nothing.

The API's side of it: `accepted_audiences` derives the other spelling of the
configured audience and accepts both. This is not a widening of who gets in —
both strings name this one API in this one tenant — and it removes the class of
failure where the only fix is another deployment.

**Nothing in the repository prints a token.** `token.sh` reports the claims
(`scripts/azure/claims.py` reads them without verifying, because verification is
the API's job and this answers a different question), `measure.sh` pipes the
token into curl, and a gate in `scripts/check.sh` fails any script that renders
one to a terminal or into a file.

## Consequences

**Positive.** Getting a token is one command, and when it does not work the
script names which of the four causes it was rather than leaving a 401 to be
interpreted. The keyless argument now holds all the way to the caller: there is
no secret on the registration, in a shell profile, or in a deployment file.
Nobody has to know that a v1.0 token would fail on the issuer, because the
registration no longer issues one.

**Negative — the tooling modifies an Entra object.** Everything else in these
scripts is ARM, reviewable as a template and removed by `destroy.sh`. This is a
PATCH to Microsoft Graph, it is not in the template because an app registration
is not an ARM resource, and `destroy.sh` does not undo it — the registration
outlives the environment deliberately, since it is the one prerequisite that is
created by hand.

**Negative — pre-authorisation is a standing grant.** Anyone who can sign in to
the tenant with the Azure CLI can obtain a token for this API without a consent
prompt. For a bounded measured-run environment with one user that is the point;
for a real deployment it would be a scope to grant deliberately and an app role
to check, rather than a registration that accepts any signed-in user of one very
widely installed client.

**Neutral — two accepted audiences, permanently.** A future reader will find an
API that accepts `api://<id>` and `<id>` and may take it for sloppiness. It is
written down here and in the function's docstring for that reason.
