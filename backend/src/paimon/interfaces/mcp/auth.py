"""Making the MCP endpoint an OAuth 2.1 resource server.

The specification is explicit about the role: a protected MCP server *is* a
resource server. It must publish Protected Resource Metadata (RFC 9728) so a
client can discover where to get a token, must validate that a token was issued
for **it** as the audience, and must not accept or forward tokens meant for
anything else.

This platform already validates bearer tokens with audience checking through
:class:`~paimon.domain.ports.IdentityProvider` (ADR-0004), so none of that is
rebuilt here. What is built is the part the protocol adds: the challenge that
tells an unauthenticated client *where to go*, and the document it goes to.

The challenge is written here rather than taken from the SDK's own middleware
for two reasons. The SDK exposes it only on a lower-level server object, and its
verifier signature returns "valid or not" — which cannot express "the identity
provider was unreachable". Answering an outage with a 401 sends the client to
re-authorize against an authorization server that cannot answer either.
"""

from collections.abc import Callable, Mapping

from mcp.server.auth.routes import build_resource_metadata_url
from mcp.server.auth.routes import create_protected_resource_routes as _create_routes
from pydantic import AnyHttpUrl
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from paimon.domain.errors import AuthenticationError, IdentityProviderUnavailableError
from paimon.domain.ports import IdentityProvider
from paimon.interfaces.mcp.gateway import bearer_token

#: Scopes this resource advertises. Empty for now: authorization here is
#: tenant isolation, which every caller gets and none can widen, so a scope
#: would describe a distinction the platform does not yet make.
SCOPES: list[str] = []

IdentityResolver = Callable[[], IdentityProvider]


def protected_resource_routes(resource_url: str, authorization_server: str) -> list[Route]:
    """Build the RFC 9728 metadata routes for this resource server.

    Mounted on the *parent* application, at the root: the specification puts this
    document at ``/.well-known/oauth-protected-resource`` with the resource's
    path appended, and that is where a client looks — not under whatever path the
    server happens to be mounted at.

    Args:
        resource_url: Canonical URI of the MCP server. It must be the address
            clients actually reach, because it is also the audience their tokens
            will name.
        authorization_server: Issuer that mints tokens for it.
    """
    return _create_routes(
        resource_url=AnyHttpUrl(resource_url),
        authorization_servers=[AnyHttpUrl(authorization_server)],
        scopes_supported=SCOPES or None,
        resource_name="Paimon",
    )


def challenge(resource_url: str) -> str:
    """Build the ``WWW-Authenticate`` value for an unauthenticated request.

    Carrying ``resource_metadata`` is the whole point: without it a 401 tells a
    client it is unwelcome, and with it the client can discover the authorization
    server and come back with a token.
    """
    return f'Bearer resource_metadata="{build_resource_metadata_url(AnyHttpUrl(resource_url))}"'


class RequireBearerToken:
    """Rejects unauthenticated requests before they reach the protocol.

    A caller with no usable token is turned away at the door with somewhere to
    go, rather than reaching a tool and receiving an error message it cannot act
    on. Authentication still happens again inside the call, which is not
    redundant: this decides whether to serve the request at all, and the gateway
    decides whose material the request may read.
    """

    def __init__(self, app: ASGIApp, identity: IdentityResolver, resource_url: str) -> None:
        """Wrap an application.

        Args:
            app: The MCP application to protect.
            identity: Resolves the identity provider at request time, because it
                belongs to process-lifetime resources the composition root owns.
            resource_url: Canonical URI of this server, for the challenge.
        """
        self._app = app
        self._identity = identity
        self._resource_url = resource_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Authenticate, then hand the request on."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        try:
            await self._identity().authenticate(bearer_token(headers))
        except IdentityProviderUnavailableError:
            # Not a 401. The caller may be perfectly legitimate; we could not
            # tell. A 401 here would send them to re-authorize against an
            # authorization server that is equally unreachable.
            await _respond(send, 503, "identity provider unavailable", {})
            return
        except AuthenticationError:
            await _respond(
                send,
                401,
                "authentication required",
                {"WWW-Authenticate": challenge(self._resource_url)},
            )
            return

        await self._app(scope, receive, send)


async def _respond(send: Send, status: int, detail: str, headers: Mapping[str, str]) -> None:
    body = f'{{"detail":"{detail}"}}'.encode()
    raw = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    raw.extend((key.encode("latin-1"), value.encode("latin-1")) for key, value in headers.items())
    await send({"type": "http.response.start", "status": status, "headers": raw})
    await send({"type": "http.response.body", "body": body})


__all__ = [
    "SCOPES",
    "IdentityResolver",
    "RequireBearerToken",
    "challenge",
    "protected_resource_routes",
]
