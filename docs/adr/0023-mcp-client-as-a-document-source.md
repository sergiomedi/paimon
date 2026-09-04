# ADR-0023: Consuming MCP servers as document sources, not as an agent's toolbox

- **Status:** Accepted
- **Date:** 2026-09-04
- **Phase:** 4 — MCP

## Context and problem statement

ADR-0020 to ADR-0022 put this platform on the server side of the Model Context
Protocol. This is the other side: Paimon as a **client**, reading from systems it does not
own.

The phase brief names three initial integrations — PostgreSQL, filesystem, GitHub — and two
of them do not survive contact with this system. The database is already reached through a
driver and a connection pool this platform owns; the filesystem an MCP filesystem server
would expose inside a container is the *container's*, which belongs to nobody. Speaking a
protocol to either would add a session, a transport and a failure mode in order to arrive
somewhere already reachable. GitHub is different in the way that matters: it is external,
it has its own authentication, and there is an official server that exposes both.

That leaves the real question. A client can consume an MCP server in two very different
ways, and the difference is not a matter of taste.

## Decision drivers

- Tool definitions are read by a model as instructions, and they arrive from outside.
- A client running inside a server is the textbook server-side request forgery gadget.
- Everything ingested is text somebody else wrote, landing where agents will read it.
- What this platform integrates with must be a property of its configuration.

## Considered options

1. **Expose the external server's tools to the agents.** The agent's toolbox grows by
   whatever the server offers.
2. **Consume the server behind a narrow port** — a source of documents, ingested like any
   other document.

## Decision

Option 2, and none of the three reasons is architectural neatness.

**Tool definitions are prompt surface.** Anthropic's own guidance on consuming MCP servers
reports loading external tool definitions into an agent's context costing 150,000 tokens
where a narrower approach costs 2,000, and that is the *cheap* problem. OWASP's description
of tool poisoning is the expensive one: a server's tool descriptions are vetted when a
client connects and are what the model reads, and a server can change them afterwards.
ADR-0018 already decided that this platform's tool surface stays small and that each tool is
written the way a prompt is written. A port that carries documents has nowhere for an
external description to go, which makes that decision hold by construction rather than by
discipline.

**A source is configuration, never a parameter.** The API lets a caller name one of the
sources this process was started with. There is no field for a URL, a repository or a
credential, because the endpoint that accepts one is the endpoint that fetches
`169.254.169.254` on request. The client refuses plaintext, private and link-local
addresses and does not follow redirects, which is what the specification asks of a client
deployed inside a server; the guard that actually closes the hole is an egress policy, and
that is written up as a deployment concern rather than pretended to be solved here.

**Tool definitions are pinned.** On every connection the client lists the server's tools and
compares a digest of each name, description and input schema against the one recorded in
configuration. A mismatch stops the synchronisation and says which tool changed. A
definition that has never been seen cannot be pinned, so the first connection logs its digest
instead of refusing — a rule that made the first connection impossible would be turned off,
and a control that is off protects nothing. The digest covers what a model would receive and
nothing else, and the schema is serialized with sorted keys, so re-ordered JSON does not
raise a false alarm.

**The endpoint is the read-only toolset.** GitHub's server scopes by URL path, so
least privilege here is one path segment: a synchronisation that cannot call `delete_file`
cannot be talked into calling it, whatever ends up in a README. A test pins the default
against the adapter's constant so the two cannot drift.

**And the boundary that all of this exists for: ingested content is data.** A document that
says "ignore all previous instructions" is indexed, not rejected — filtering on the phrase
would break every runbook that quotes an incident and would still miss the next wording.
What is guaranteed instead is *where the text may go*: it becomes a document's text, is
retrieved as a numbered source, and reaches a model inside the user turn while the system
turn stays this platform's own prompt. Nothing a source returns becomes a tool description.
Those two invariants are asserted as tests rather than described in prose, because a boundary
nothing checks is a boundary that moves.

## Consequences

**Positive.** A corpus can now follow a repository instead of being pushed at the platform
document by document, and it does so through the same pipeline as everything else — the
content hash means a scheduled run over an unchanged repository costs no embeddings. The port
takes documents, so a source reading a mounted directory or an object store is a new adapter
and nothing above it moves.

**Negative.** The adapter is coupled to another team's tool names and result shapes, and it
will break on a change this platform does not control. That is the price of not
reimplementing GitHub's API, and it is contained: one file, behind a port, with a stub server
in its tests. The stub is a faithful simulation of the shapes, not proof they are still what
GitHub sends.

**Not done here.** A synchronisation runs inline in the request, which is honest at the
document ceiling in configuration and stops being honest above it. A scheduled worker is
Phase 7's, and the ceiling is what keeps the difference from arriving as a surprise. There is
also no per-tool audit log yet — security guidance for MCP consumers asks for one, and it
belongs with the tracing work in Phase 5 rather than as a second logging scheme now.

**Refused deliberately.** PostgreSQL and filesystem MCP servers, for the reasons above. A
"no" with a reason is worth more here than a third integration that exists to be counted.
