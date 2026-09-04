# ADR-0021: The MCP endpoint is an OAuth 2.1 resource server, and says so

- **Status:** Accepted
- **Date:** 2026-09-04
- **Phase:** 4 — MCP

## Context and problem statement

ADR-0020 left the MCP endpoint authenticating inside the tool call: a caller with no token
reached a tool and got an error message back. That works and is not compliant, and the gap
matters more than it sounds.

The specification is direct about the role — a protected MCP server **is** an OAuth 2.1
resource server. It must publish Protected Resource Metadata (RFC 9728) so a client can
discover where to obtain a token, must validate that a token was issued for **it** as the
audience, and *"MUST NOT accept or transit any other tokens"*.

The practical consequence is discovery. An MCP client that meets a new server does not know
which authorization server to talk to; the protocol's answer is that the server tells it, in a
`401` whose `WWW-Authenticate` header points at a metadata document. Without that, a caller
without a token is simply stuck — and "stuck" is what this platform was doing.

## Decision drivers

- One authentication path for the platform; ADR-0004 already validates audience.
- A client that lacks a token must be told where to get one.
- "Not who you say you are" and "we could not tell" must stay distinguishable.
- A deployment with no real authorization server must not advertise one.

## Considered options

1. **Use the SDK's `AuthSettings` and `TokenVerifier`** on the transport.
2. **Implement the challenge and the metadata routes at this platform's own layer.**
3. **Leave it as it is**, and document the non-compliance.

## Decision

Option 2, and the reasons are specific rather than a preference for writing more code.

The SDK does implement all of this — but only on its lower-level server object. `MCPServer`,
the class this platform uses, does not forward `auth` or `token_verifier`, so reaching them
means reaching into a private attribute.

More decisively, its `TokenVerifier` protocol returns a token *or `None`*. There is no way to
say "the identity provider was unreachable". Collapsing that into `None` answers an outage with
a `401`, which sends the client to re-authorize against an authorization server that is very
likely unreachable for the same reason. ADR-0004 separated those two cases deliberately, and
adopting an interface that cannot express the difference would quietly undo it. Here an
unreachable provider is a `503`.

So: an ASGI middleware wraps the mounted transport, rejects a request with no usable token, and
answers with `WWW-Authenticate: Bearer resource_metadata="…"`. The metadata routes come from
the SDK's own public builder — no reason to hand-write a document the SDK already models — and
are registered on the **parent** application at the root, because RFC 9728 places the document
at `/.well-known/oauth-protected-resource` with the resource's path appended, and that is where
a client looks. Registered inside the mount they would sit at `/mcp/.well-known/…`, which is
correct-looking and undiscoverable.

**Metadata is published only when a deployment has an authorization server to name.** The
development identity provider is not an OAuth issuer; advertising one that does not exist would
send clients somewhere useless. Unconfigured, the endpoint behaves as it did in ADR-0020.

Authentication now happens twice per request — once at the door, once in the gateway — and that
is not redundancy. The middleware decides whether to serve the request at all; the gateway
decides *whose* material it may read. Removing either leaves a real hole: without the first, an
anonymous caller reaches a tool; without the second, an authenticated caller from one
organization reads another's corpus.

## Consequences

**Positive.** A client that has never seen this server can now bootstrap: it gets a `401`,
follows `resource_metadata`, discovers the authorization server and comes back with a token
minted for the right audience. The `503` case survives contact with the protocol, which it
would not have through the SDK's verifier.

**Negative.** The metadata endpoint and the challenge are this platform's code, so an update to
the specification is this platform's problem rather than a dependency bump. The mitigation is
that both are small and both are tested against the exact paths and header shape the
specification names.

**Not done here.** The resource URL is configuration, and a deployment that sets it to the
container's address rather than the public one will mint tokens for an audience the server does
not accept, with no useful error at either end. That is a deployment concern for Phase 7, and
it is called out in `.env.example` where someone setting the value will read it.
