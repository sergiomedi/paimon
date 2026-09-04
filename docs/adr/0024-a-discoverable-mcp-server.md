# ADR-0024: The MCP server describes itself, at a path that is still an argument

- **Status:** Accepted
- **Date:** 2026-09-04
- **Phase:** 4 — MCP

## Context and problem statement

ADR-0021 made the endpoint discoverable in one narrow sense: a client that already knows the
URL and gets a `401` can find out where to obtain a token. It cannot find out that the
server exists, what it is for, or which transport it speaks. Those are the questions a
registry and a client's "add a server" flow ask, and this platform answered none of them.

The ecosystem's answer is `server.json` — the schema the official MCP Registry publishes
and consumes, carrying a reverse-DNS name, a description, a version and the remotes a client
can dial. Deployments that list themselves in the registry serve it from their own domain,
which is how a registry re-checks a listing rather than trusting a snapshot.

The complication is that **where** to serve it is not settled. Two proposals are open —
`/.well-known/mcp/server-card.json` and `/.well-known/mcp` — and they disagree on both the
path and the emphasis. Neither is in the specification.

## Decision drivers

- A server nobody can discover is a server nobody connects to.
- A discovery document that requires a token cannot be used for discovery.
- Implementing an unratified proposal as though it were settled is a lie in code.
- A document naming the wrong address is worse than no document.

## Considered options

1. **Serve nothing** until a well-known path is ratified.
2. **Serve the document** at the path deployments use today, and say what is convention.
3. **Serve both proposed paths**, on the theory that one will win.

## Decision

Option 2.

The distinction that makes this comfortable is between the **artefact** and the **address**.
`server.json` is versioned, published, and consumed by the registry today; it is the thing
that will outlive the argument about paths. This platform serves it at
`/.well-known/mcp/server.json`, which is what deployments publishing to the registry
currently use, and the module says in as many words that the path is convention rather than
specification. When a proposal is ratified, moving is one constant.

Option 3 is the version of this that looks thorough and is not. Serving two documents that
disagree in emphasis means maintaining two, and being wrong twice when the ratified shape
matches neither.

**Built from settings, not from a checked-in file.** The document names the URL clients
reach, and that value already exists as `mcp.resource_url` because it is also the audience
tokens are minted for. Deriving one from the other means they cannot disagree; a static file
would eventually name a staging host in production.

**Published only when that URL is configured.** A deployment that has not set it would
otherwise advertise a container's internal address to everything that reads the document,
which is worse than not being discoverable — a client that cannot find you tries something
else, and a client sent to the wrong address fails in ways nobody can debug from outside.

**Unauthenticated, and correspondingly thin.** Requiring a token to learn where to send a
token is a loop. So the document says what a stranger may know — the address, the transport,
the header — and lists no tools, no tenants and no corpus. A test pins the exact set of keys,
because "we only publish what is public" decays the first time somebody adds a field.

**Truthful about authentication.** A deployment with no authorization server configured does
not ask for an `Authorization` header in the document, because there is nowhere to obtain
one. Describing a door with no key is not a security measure.

## Consequences

**Positive.** Paimon can be listed in the official registry without a code change; that is a
publish step waiting on a public URL, which is Phase 7. A client's "add a server" flow has
something to read. And the two well-known documents now compose: one says where the server
is, the other says where its tokens come from.

**Negative.** The path is a bet on a convention. If a ratified proposal picks the other, this
serves a document at an address nothing looks at until it is moved — a one-line change, but a
real one, and it is called out here so it is found rather than discovered.

**Not done here.** Nothing validates the document against the published JSON schema in CI.
Fetching a schema over the network in a test buys a flaky build; pinning a copy buys a copy
that goes stale. The version is asserted, the keys are asserted, and the rest waits for the
schema to be worth vendoring.
