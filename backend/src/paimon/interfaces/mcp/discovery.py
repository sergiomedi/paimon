"""Describing this MCP server so that something else can find it.

A server nobody can discover is a server nobody connects to, and the ecosystem's
answer to that is ``server.json`` — the schema the official MCP Registry
publishes and consumes. This module builds that document from the same settings
the server itself is built from, so the URL a client is told to use is the URL
the server is actually mounted at.

**Where it is served is less settled than what it contains.** The document is
stable and versioned; the well-known path for fetching it is still two competing
proposals — ``/.well-known/mcp/server-card.json`` and ``/.well-known/mcp``. This
platform serves ``/.well-known/mcp/server.json``, which is what deployments
publishing to the registry today use, and says plainly in ADR-0024 that the path
is convention rather than specification. The artefact that will outlive the
argument is the document.

Unauthenticated, and deliberately. It names the endpoint and the header a client
must present; it lists no tools, no tenants and no corpus. Requiring a token to
learn where to send a token is a loop.
"""

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

#: Versioned schema this document claims to satisfy.
SCHEMA_URL = "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"

#: Reverse-DNS name, as the registry requires. It is an identity, not a URL, and
#: it stays the same across deployments of the same server.
SERVER_ID = "io.github.sergiomedi/paimon"

DISCOVERY_PATH = "/.well-known/mcp/server.json"

DESCRIPTION = (
    "Grounded search over an engineering organization's operational documentation, "
    "and the operational agents that reason over it."
)


def server_json(*, resource_url: str, version: str, authenticated: bool) -> dict[str, Any]:
    """Build the discovery document for this deployment.

    Args:
        resource_url: The address clients actually reach this server at.
        version: The platform's version, as its package reports it.
        authenticated: Whether the endpoint requires a bearer token. A deployment
            running the development identity provider does not publish OAuth
            metadata, and advertising a header nobody can obtain a value for
            would be worse than saying nothing.

    Returns:
        A ``server.json`` document.
    """
    remote: dict[str, Any] = {"type": "streamable-http", "url": resource_url}
    if authenticated:
        remote["headers"] = [
            {
                "name": "Authorization",
                "description": (
                    "Bearer token for this server as the audience. Where to obtain one is "
                    "published at /.well-known/oauth-protected-resource (RFC 9728)."
                ),
                "isRequired": True,
                "isSecret": True,
            }
        ]
    return {
        "$schema": SCHEMA_URL,
        "name": SERVER_ID,
        "title": "Paimon",
        "description": DESCRIPTION,
        "version": version,
        "remotes": [remote],
    }


def discovery_routes(*, resource_url: str, version: str, authenticated: bool) -> list[Route]:
    """Serve the discovery document at the well-known path.

    Args:
        resource_url: The address clients actually reach this server at.
        version: The platform's version.
        authenticated: Whether a bearer token is required.

    Returns:
        The route to register on the parent application, at the root.
    """
    document = server_json(resource_url=resource_url, version=version, authenticated=authenticated)

    async def serve(request: Request) -> JSONResponse:
        _ = request
        return JSONResponse(
            document,
            # nosniff because a browser deciding for itself what this JSON is
            # can be talked into treating it as something executable.
            headers={"Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff"},
        )

    return [Route(DISCOVERY_PATH, serve, methods=["GET"], name="mcp_server_json")]


__all__ = [
    "DESCRIPTION",
    "DISCOVERY_PATH",
    "SCHEMA_URL",
    "SERVER_ID",
    "discovery_routes",
    "server_json",
]
