"""A client for MCP servers this platform does not own.

The platform is on both sides of the protocol now. ``interfaces/mcp`` is the
server it presents to the outside; this is the client it points at somebody
else's, and the two have almost nothing in common beyond the wire format. A
server decides what to expose and to whom. A client decides **what to trust**,
and that is the whole of what this module is about.

Three controls, each answering something specific:

*Where it may connect.* The specification is explicit that a client running
inside a server has to consider SSRF, because a URL it is told to fetch may point
at ``169.254.169.254`` and hand a cloud provider's instance credentials to
whoever asked. So an endpoint is HTTPS, is not a private or link-local address,
and redirects are not followed.

*That the server is still the server it was.* Tool definitions are what a model
reads, and a server can change them between one session and the next. Fingerprints
recorded in configuration are compared on every connection, and a mismatch stops
the run rather than quietly working with a description nobody reviewed.

*That its output is data.* A tool result comes back as text and goes into a
document. It is never a description, never a prompt, and never an instruction —
which is a property of where the value is allowed to flow, not of anything this
module can detect by looking at it.
"""

import hashlib
import ipaddress
import json
import socket
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx2
import structlog
from mcp import types
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from paimon.domain.errors import (
    SourceContentError,
    SourceUnavailableError,
    UntrustedSourceError,
)

logger = structlog.get_logger(__name__)

#: Opening a session, and the calls made through it, share this budget. Long
#: enough for a large file over a slow link, short enough that a synchronisation
#: against an unresponsive server fails instead of hanging a worker.
DEFAULT_TIMEOUT_SECONDS = 30.0

Connect = Callable[[], AbstractAsyncContextManager[Client]]


def fingerprint(tool: types.Tool) -> str:
    """Digest a tool's definition as a model would receive it.

    Name, description and input schema, and nothing else. Those three are what
    reaches a model and what an attacker would need to change to redirect it;
    the icons and annotations around them do not, so including them would make
    the fingerprint change for reasons that do not matter.

    The schema is serialized with sorted keys, because a server re-ordering its
    JSON is not a change to what the schema says, and a check that fired on that
    would be turned off within a week.
    """
    schema = json.dumps(tool.input_schema, sort_keys=True, separators=(",", ":"))
    material = f"{tool.name}\n{tool.description or ''}\n{schema}"
    return hashlib.sha256(material.encode()).hexdigest()


def verify_url(url: str, *, allow_loopback: bool = False) -> None:
    """Refuse an endpoint a server-side client should not be dialling.

    Args:
        url: The endpoint to check.
        allow_loopback: Permit ``http://`` on loopback, for local development
            against a server running on the same machine.

    Raises:
        SourceUnavailableError: The URL is not one this client will connect to.

    Note:
        Name resolution here and the connection later are two separate moments,
        and a hostile DNS answer can differ between them. This narrows the
        obvious cases; the control that actually closes it is an egress policy
        that cannot see the private ranges at all, which is a deployment
        concern and is written up as one.
    """
    parts = urlsplit(url)
    host = parts.hostname
    if host is None:
        msg = f"source endpoint '{url}' has no host"
        raise SourceUnavailableError(msg)

    loopback = host in {"localhost", "127.0.0.1", "::1"}
    if parts.scheme != "https" and not (allow_loopback and loopback and parts.scheme == "http"):
        msg = f"source endpoint '{url}' must use https"
        raise SourceUnavailableError(msg)
    if loopback and allow_loopback:
        return

    for address in _resolve(host):
        if address.is_private or address.is_loopback or address.is_link_local:
            msg = (
                f"source endpoint '{url}' resolves to {address}, which is not a public address; "
                "an external source is not reachable at a private one"
            )
            raise SourceUnavailableError(msg)


def _resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Return every address a hostname resolves to, or itself if it is one."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as error:
        msg = f"source endpoint host '{host}' could not be resolved: {error}"
        raise SourceUnavailableError(msg) from error
    return [ipaddress.ip_address(str(info[4][0])) for info in infos]


def http_endpoint(
    url: str,
    token: str | None = None,
    *,
    allow_loopback: bool = False,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> Connect:
    """Build a connector for a remote MCP server over streamable HTTP.

    Args:
        url: The server's endpoint.
        token: Bearer credential, if the server requires one.
        allow_loopback: Permit an ``http://`` loopback endpoint.
        timeout_seconds: Budget for the session and the calls through it.

    Returns:
        Something a :class:`McpToolClient` can open a session with.
    """

    @asynccontextmanager
    async def connect() -> AsyncIterator[Client]:
        verify_url(url, allow_loopback=allow_loopback)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with (
            # Redirects are not followed. A server that answers a tool call with
            # a redirect is either broken or pointing this client somewhere it
            # did not agree to go, and neither is worth accommodating.
            httpx2.AsyncClient(
                headers=headers, follow_redirects=False, timeout=timeout_seconds
            ) as http,
            Client(
                streamable_http_client(url, http_client=http),
                raise_exceptions=True,
                read_timeout_seconds=timeout_seconds,
            ) as client,
        ):
            yield client

    return connect


def _sole_cause(error: BaseException) -> BaseException:
    """Peel task-group wrappers off a single failure.

    A group carrying one exception is plumbing around one error. A group
    carrying several is several errors, and is returned as it stands.
    """
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error


@dataclass(frozen=True, slots=True)
class ToolContract:
    """What this platform expects a server to still be offering.

    Attributes:
        name: The tool this platform calls.
        digest: The fingerprint recorded when the definition was reviewed, or
            None if it has not been pinned yet.
    """

    name: str
    digest: str | None = None


class McpToolClient:
    """Opens sessions with one external MCP server and calls agreed tools on it."""

    def __init__(self, connect: Connect, contracts: Sequence[ToolContract], *, server: str) -> None:
        """Initialise the client.

        Args:
            connect: How to open a session. A remote endpoint in a deployment; a
                server object in a test, which is what lets the protocol itself
                be exercised without HTTP.
            contracts: The tools this platform calls, and the fingerprints it
                agreed to. A contract without one is checked for existence only,
                and its digest is logged so an operator can pin it.
            server: Name for this server in logs and errors.
        """
        self._connect = connect
        self._contracts = tuple(contracts)
        self._server = server

    @asynccontextmanager
    async def session(self) -> AsyncIterator["McpSession"]:
        """Open a verified session.

        Yields:
            A session whose tools have been checked against their contracts.

        Raises:
            SourceUnavailableError: The server could not be reached.
            UntrustedSourceError: The server no longer offers what it did.
        """
        try:
            async with self._connect() as client:
                await self._verify(client)
                yield McpSession(client, self._server)
        except BaseExceptionGroup as group:
            # The transport runs inside a task group, so anything raised in here
            # comes back out wrapped. Unwrapping it is not cosmetic: a caller
            # that has to catch an ExceptionGroup to find out its source was
            # unreachable will catch too much, and one day that will swallow a
            # bug. Only a lone cause is unwrapped; a genuine group of several
            # failures is not one error and is not pretended to be.
            raise _sole_cause(group) from group
        except (MCPError, httpx2.HTTPError, OSError) as error:
            msg = f"mcp server '{self._server}' could not be reached: {error}"
            raise SourceUnavailableError(msg) from error

    async def _verify(self, client: Client) -> None:
        """Check the server still offers the tools this platform agreed to."""
        offered = {tool.name: tool for tool in (await client.list_tools()).tools}
        for contract in self._contracts:
            tool = offered.get(contract.name)
            if tool is None:
                msg = (
                    f"mcp server '{self._server}' does not offer tool '{contract.name}'; "
                    f"it offers: {', '.join(sorted(offered)) or 'nothing'}"
                )
                raise UntrustedSourceError(msg)
            digest = fingerprint(tool)
            if contract.digest is None:
                # Not a failure. You cannot pin a definition you have never seen,
                # and refusing to work until someone does would mean the first
                # connection can never happen. The digest is logged at the level
                # an operator reads, so pinning it is a copy and a redeploy.
                logger.info(
                    "mcp_tool_unpinned",
                    server=self._server,
                    tool=contract.name,
                    digest=digest,
                )
                continue
            if digest != contract.digest:
                msg = (
                    f"mcp server '{self._server}' changed the definition of '{contract.name}': "
                    f"expected {contract.digest}, found {digest}. A server whose tool "
                    "descriptions change is either updated or compromised, and from here "
                    "those look the same"
                )
                raise UntrustedSourceError(msg)


class McpSession:
    """One open session with an external server."""

    def __init__(self, client: Client, server: str) -> None:
        """Wrap a connected client."""
        self._client = client
        self._server = server

    async def call(self, name: str, arguments: Mapping[str, Any]) -> str:
        """Call a tool and return its result as text.

        Args:
            name: The tool to call.
            arguments: Its arguments.

        Returns:
            The text the server returned, joined if it came in several parts.

        Raises:
            SourceUnavailableError: The call could not be made.
            SourceContentError: The server reported the call as failed, or
                answered with something that is not text.
        """
        try:
            result = await self._client.call_tool(name, dict(arguments))
        except (MCPError, httpx2.HTTPError, OSError) as error:
            msg = f"mcp server '{self._server}' failed calling '{name}': {error}"
            raise SourceUnavailableError(msg) from error

        text = "\n".join(
            block.text for block in result.content if isinstance(block, types.TextContent)
        )
        if result.is_error:
            # The server's own words, and they are quoted rather than acted on.
            msg = f"mcp server '{self._server}' rejected '{name}': {text or 'no reason given'}"
            raise SourceContentError(msg)
        if not text:
            msg = f"mcp server '{self._server}' returned no text for '{name}'"
            raise SourceContentError(msg)
        return text


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "Connect",
    "McpSession",
    "McpToolClient",
    "ToolContract",
    "fingerprint",
    "http_endpoint",
    "verify_url",
]
