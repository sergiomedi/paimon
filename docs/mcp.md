# Paimon over the Model Context Protocol

Paimon is on both sides of MCP.

- **As a server**, it exposes its corpus and its agents to any MCP client — Claude, an IDE,
  another agent — as three tools, behind OAuth 2.1.
- **As a client**, it reads documentation out of systems it does not own and indexes it,
  starting with GitHub through GitHub's official MCP server.

The two are separate concerns and this document keeps them separate.

---

## Part 1 — Paimon as an MCP server

### What it offers

| Tool | What it does |
|---|---|
| `search_corpus` | Hybrid search over the caller's corpus. Returns passages with the document, the heading path and the character offsets they came from. |
| `read_document` | Returns a whole document by id, for when a passage is not enough. |
| `run_agent` | Runs one of the platform's agents to completion and returns its answer together with the steps it took. |

`run_agent` is the one worth reaching for. `search_corpus` hands back passages that the
calling model then has to reason over; an agent has already framed the question two ways,
retrieved against each, checked that its draft is supported by what it found, and refused
to answer when it was not. Delegating the task beats fetching the material
([ADR-0022](adr/0022-agents-as-mcp-tools.md)).

**Documents are not offered as MCP resources**, and that is a limitation rather than a
choice — the reasoning is in `interfaces/mcp/server.py`, next to where the resource would
go, and a test asserts the absence so it is not mistaken for an oversight.

### The endpoint

```
POST https://<your-deployment>/mcp
```

Streamable HTTP, stateless, at the root of the mount. It sits **outside** the `/api/v1`
prefix on purpose: the protocol carries its own version, and a client that has to guess
which of our prefixes an MCP endpoint sits behind is a client we made work for no reason.

Every call is authenticated twice and neither is redundant: the middleware at the door
decides whether to serve the request at all, and the gateway inside decides *whose*
material it may read ([ADR-0021](adr/0021-mcp-as-an-oauth-resource-server.md)).

### Discovery

A client that has never met this server can find out about it without a token:

| Path | Document |
|---|---|
| `/.well-known/mcp/server.json` | `server.json` — the endpoint, the transport, and the header to present. |
| `/.well-known/oauth-protected-resource/mcp` | RFC 9728 protected resource metadata — which authorization server mints tokens for it. |

Both are published only when `PAIMON_MCP__RESOURCE_URL` is set, and the second additionally
requires `PAIMON_MCP__AUTHORIZATION_SERVER`. A deployment running the development identity
provider advertises nothing, because it is not an OAuth issuer and pointing clients at one
that does not exist is worse than silence ([ADR-0024](adr/0024-a-discoverable-mcp-server.md)).

### Connecting Claude

Claude reaches remote MCP servers through **Custom Connectors**, not through
`claude_desktop_config.json` — that file is for local servers started as a subprocess.

1. Open Claude, go to **Settings → Connectors**.
2. **Add custom connector**, and give it the full `https://` URL of the endpoint above.
3. Complete authentication. Claude follows the `401`, reads the metadata document, and
   sends you to the authorization server named in it.
4. In the connector's settings, enable only the tools you want reachable.

**Connectors require a public HTTPS URL**, so a server running on `localhost:8000` cannot be
added this way. For local work, use one of these instead.

### Trying it locally

The MCP Inspector is the fastest way to see the tools, call one, and read the raw protocol
traffic:

```bash
# Terminal 1 — the platform
cd backend && uv run uvicorn paimon.interfaces.api:app --reload

# Terminal 2 — mint a token with the development signer (see the README), then
# open the Inspector against the endpoint
export PAIMON_TOKEN=$(cd backend && uv run python -c "
from paimon.infrastructure.identity import DevIdentityProvider
from paimon.config import get_settings
print(DevIdentityProvider(get_settings().auth.dev_signing_key.get_secret_value()).issue(
    subject='you', tenant_id='local', display_name='You'))")
npx @modelcontextprotocol/inspector
```

In the Inspector choose **Streamable HTTP**, enter `http://localhost:8000/mcp`, and add an
`Authorization: Bearer $PAIMON_TOKEN` header.

To reach a local server from Claude itself, put a public HTTPS address in front of it — a
tunnel is enough — and set `PAIMON_MCP__RESOURCE_URL` to *that* address. The value is also
the audience tokens will name, so a mismatch here is rejected with no useful explanation at
either end.

### Publishing to the registry

The discovery document is already the shape the official MCP Registry consumes, so listing
Paimon there is a publish step rather than a code change. It waits for Phase 7, because a
registry entry pointing at a URL that does not exist yet is an entry nobody can use.

---

## Part 2 — Paimon as an MCP client

Paimon reads documentation out of GitHub through GitHub's official MCP server, and indexes
what it finds like any other document ([ADR-0023](adr/0023-mcp-client-as-a-document-source.md)).

### Configuring a source

```bash
# The READ-ONLY repository toolset. A synchronisation that cannot call
# delete_file cannot be talked into calling it, whatever ends up in a README.
PAIMON_SOURCES__GITHUB_ENDPOINT=https://api.githubcopilot.com/mcp/x/repos/readonly
PAIMON_SOURCES__GITHUB_TOKEN=<a token with read access to the repositories below>

PAIMON_SOURCES__GITHUB='[{"name":"handbook","owner":"acme","repo":"handbook","paths":["docs"],"suffixes":[".md"],"max_depth":4,"max_documents":200}]'
```

`paths` matters more than it looks. Left empty the walk starts at the repository root,
which on a repository that also holds code is a long and expensive way to index nothing
useful.

### Running a synchronisation

```bash
curl -s localhost:8000/api/v1/sources -H "Authorization: Bearer $PAIMON_TOKEN"
# {"sources":["handbook"]}

curl -s -X POST localhost:8000/api/v1/sources/handbook/synchronizations \
     -H "Authorization: Bearer $PAIMON_TOKEN"
# {"source":"handbook","considered":24,"indexed":24,"unchanged":0,"failed":[]}
```

Run it again and everything comes back `unchanged`: ingestion compares a content hash, so a
scheduled synchronisation over an untouched repository costs no embeddings.

A caller **names** a source; it cannot describe one. There is no field for a URL, a
repository or a credential, because the endpoint that accepts one is the endpoint that
fetches `169.254.169.254` on request.

### Pinning the server's tools

Tool descriptions are what a model reads, and a server can change them between one session
and the next. Paimon fingerprints each tool it calls and compares on every connection.

The first connection cannot compare against anything, so it logs what it saw:

```
mcp_tool_unpinned server=https://api.githubcopilot.com/... tool=get_file_contents digest=9f2c…
```

Put that digest in the source's configuration:

```json
{"name":"handbook","owner":"acme","repo":"handbook","paths":["docs"],
 "pinned_tools":{"get_file_contents":"9f2c…"}}
```

From then on a changed description stops the synchronisation with a `502` naming the tool.
That is deliberately not a `503`: an external server whose definitions changed under us is
not a transient failure to retry through, it is something a person has to look at.

### What is refused, and why

MCP servers for PostgreSQL and for the filesystem are **not** used. The database is already
reached through a driver and a pool this platform owns, and the filesystem an MCP filesystem
server would expose inside a container is the container's, which belongs to nobody. Speaking
a protocol to arrive somewhere already reachable adds a session, a transport and a failure
mode, and buys nothing.

### The boundary

Everything a source brings in is text somebody else wrote, landing in a corpus that agents
read. A document that says *"ignore all previous instructions"* is **indexed, not
rejected** — filtering on the phrase would break every runbook that quotes an incident and
would still miss the next wording.

What is guaranteed is where that text may go, and it is asserted as tests rather than
described here:

- It becomes a document's text, is retrieved as a numbered source, and reaches a model
  inside the **user** turn. The system turn stays this platform's own prompt.
- Nothing a source returns ever becomes a tool description.

A boundary nothing checks is a boundary that moves.
