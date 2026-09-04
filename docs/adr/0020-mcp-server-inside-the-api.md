# ADR-0020: The MCP server is an interface, mounted inside the API

- **Status:** Accepted
- **Date:** 2026-09-04
- **Phase:** 4 — MCP

## Context and problem statement

Phase 4 exposes the platform over the Model Context Protocol. Two questions have to be
answered before any code: where the server sits in this codebase, and whether it runs in this
process.

The protocol itself moved while this project was being written. The **2026-07-28**
specification removed the `initialize` handshake and the `Mcp-Session-Id` header and made the
core stateless: every request carries its own protocol version, client identity and
capabilities. The stated reason is that a request can then land on any instance behind a plain
round-robin load balancer with no shared storage — which is exactly the property this platform
wants for Phase 7. The older HTTP+SSE transport is deprecated with a migration window, and the
Python SDK's `FastMCP` is now `MCPServer`.

The brief lists *PostgreSQL, Filesystem, GitHub* as initial tools. Those describe MCP from the
**client** side: consuming off-the-shelf servers. A PostgreSQL MCP server over this platform's
own database would duplicate what the ports already do, and a filesystem server has no place in
a service. The half that carries the project is the opposite one — Paimon as a server — and the
client half is scoped to GitHub, where ingesting a repository's runbooks is a real use rather
than a checkbox.

## Decision drivers

- One definition of what a tool is, not one per consumer.
- The tenant must come from an authenticated identity, never from a tool argument.
- A deployment should be one unit until there are numbers that say otherwise.
- The tools already exist (ADR-0018); this should spend that work, not repeat it.

## Considered options

1. **Mount the server inside the API process, under a path.**
2. **Run it as a separate service**, sharing the domain and adapters as a library.
3. **stdio only**, launched by each client as a subprocess.

## Decision

Option 1. An MCP server is an *interface*, exactly like the HTTP API: same layer, same
composition root, same identity provider. It lives in `interfaces/mcp/` and is mounted at
`/mcp` — outside the versioned API prefix, because the protocol carries its own version and a
client should not have to guess which prefix an MCP endpoint sits behind.

Option 3 is rejected outright, and the reason is the interesting one: **stdio carries no
headers**, so it carries no bearer token. A server whose entire job is answering from one
organization's corpus cannot establish which organization is asking over a transport that
cannot say. The gateway refuses a request with no headers in exactly those words.

The server exposes `search_corpus` and `read_document` **from the declarations in
`paimon.agents.tools`** — the same objects a model provider is offered and the same ones the
executor runs. That was the argument for making them data in ADR-0018, and this is where it
pays: one definition, three consumers.

**Every call is authenticated separately**, and the tenant comes from the token. The protocol is
stateless, so there is no session to hang an identity on — which is the better arrangement
anyway: an identity established once and reused is an identity that outlives the token that
proved it. Over MCP the client is literally a model, so an executor taking its tenant from a
tool argument would take it from something a prompt can talk into changing.

Two smaller decisions worth recording because both were found by running it.

**The mounted application's lifespan must be entered by the parent.** Starlette does not run a
mounted app's lifespan, and the MCP transport keeps its session manager there; without it every
MCP request fails on a manager that was never started.

**Host header validation is on, and configured.** The SDK rejects requests whose `Host` it was
not told about — protection against DNS rebinding, where a page is made to resolve an
attacker's hostname to a loopback address and then talk to a local server. The shipped list
covers local development, a deployment behind a proxy lists its own names, and an empty list is
refused at startup rather than quietly allowing everything.

A tool failure is returned to the caller as a message rather than raised. The client here is a
model: `No document 'x' is indexed` is something it can act on, while a traceback is something
it will paraphrase into a claim about the corpus.

## Consequences

**Positive.** One deployment unit, one authentication path, one set of connection pools. Any
MCP-capable client — a desktop assistant, an IDE, another agent — can search this corpus and
read its documents, which is what the README has claimed the platform is for since Phase 1.

**Negative.** MCP traffic and API traffic share a process and its pools, so a burst of one
degrades the other. That is recorded as a known gap rather than solved: splitting it before
there are numbers to size it with would be guessing, and Phase 7 is where the numbers arrive.

**Not done here.** The server does not yet advertise protected resource metadata (RFC 9728) or
answer an unauthenticated request with the `WWW-Authenticate` challenge the specification
requires — it authenticates inside the tool call instead, so an anonymous caller gets an error
message rather than a 401 that tells them where to get a token. The SDK supports the compliant
path through a token verifier, and the next batch adopts it.
