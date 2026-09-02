"""Authenticating to Azure services.

Two mechanisms, chosen by configuration (ADR-0014). Headers are produced per
request rather than baked into a client at construction: a bearer token expires,
and a client built once with a token that has since lapsed fails in a way that
looks like a permissions problem rather than an expiry.
"""

import asyncio
import time
from typing import Any, Protocol, runtime_checkable

from paimon.domain.errors import DomainError


class AccessToken(Protocol):
    """The shape of a token, as azure-identity returns it.

    Read-only properties rather than attributes: a protocol's attributes are
    invariant, and azure's own AccessToken would then fail to satisfy it over a
    difference in declaration style rather than in behaviour.
    """

    @property
    def token(self) -> str:
        """The bearer token."""
        ...

    @property
    def expires_on(self) -> int:
        """Unix time at which the token stops being valid."""
        ...


@runtime_checkable
class TokenProvider(Protocol):
    """Anything that can mint a token for a scope.

    Structural rather than a dependency on azure-identity's own type: the adapter
    needs one method, and typing against the shape keeps the optional package
    optional for the type checker as well as at runtime.
    """

    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        """Return a token for the given scopes."""
        ...


# Refresh a little before the token actually expires, so a request never leaves
# with a token that dies in flight.
_EXPIRY_MARGIN_SECONDS = 300


class AzureAuthenticationError(DomainError):
    """A credential could not be obtained."""


@runtime_checkable
class AzureCredential(Protocol):
    """Produces the headers that authenticate one request."""

    async def headers(self) -> dict[str, str]:
        """Return headers to attach to a request.

        Raises:
            AzureAuthenticationError: If a credential could not be obtained.
        """
        ...


class ApiKeyCredential:
    """A static key, sent in the header the service expects.

    Azure OpenAI and Azure AI Search both read ``api-key``; the header is a
    parameter because nothing guarantees the next Azure service will.
    """

    def __init__(self, key: str, header: str = "api-key") -> None:
        """Initialise the credential.

        Args:
            key: The service key.
            header: Header name to send it in.
        """
        self._key = key
        self._header = header

    async def headers(self) -> dict[str, str]:
        """Return the key header."""
        return {self._header: self._key}


class EntraCredential:
    """A bearer token from Microsoft Entra ID.

    Wraps any azure-identity credential, so the same code path covers a managed
    identity in Azure and a developer's ``az login`` locally — which is the point
    of DefaultAzureCredential and the reason this is worth supporting at all: it
    removes service keys from configuration entirely.

    Tokens are cached until shortly before they expire. The underlying credential
    caches too, but its call is blocking, and this class is used from the request
    path.
    """

    def __init__(self, credential: TokenProvider, scope: str) -> None:
        """Initialise the credential.

        Args:
            credential: An azure-identity credential.
            scope: The scope to request, for example
                ``https://cognitiveservices.azure.com/.default``.
        """
        self._credential = credential
        self._scope = scope
        self._token: str | None = None
        self._expires_at = 0.0

    async def headers(self) -> dict[str, str]:
        """Return the bearer header, refreshing the token when it is close to expiry."""
        now = time.time()
        if self._token is None or now >= self._expires_at - _EXPIRY_MARGIN_SECONDS:
            try:
                token = await asyncio.to_thread(self._credential.get_token, self._scope)
            except Exception as error:
                msg = f"could not obtain an Entra ID token for {self._scope}: {error}"
                raise AzureAuthenticationError(msg) from error
            self._token = token.token
            self._expires_at = float(token.expires_on)
        return {"Authorization": f"Bearer {self._token}"}


def build_credential(
    api_key: str | None, scope: str, *, header: str = "api-key"
) -> AzureCredential:
    """Build the credential a service should use.

    A key when one is configured, Entra ID otherwise. Falling back to Entra rather
    than failing is deliberate: it means a deployment removes its keys by deleting
    them from configuration, not by also remembering to flip a mode.

    Args:
        api_key: The service key, if configured.
        scope: Entra scope to request when no key is configured.
        header: Header to send an API key in.

    Returns:
        A credential.

    Raises:
        AzureAuthenticationError: If no key is configured and azure-identity is
            not installed.
    """
    if api_key:
        return ApiKeyCredential(api_key, header=header)
    try:
        # Imported here on purpose: azure-identity is an optional dependency,
        # and a module-level import would make the whole package unimportable
        # for a deployment that only ever uses API keys.
        from azure.identity import DefaultAzureCredential  # noqa: PLC0415
    except ImportError as error:  # pragma: no cover - depends on the install
        msg = (
            "no API key configured and azure-identity is not installed; "
            "install the 'azure' extra or set the service key"
        )
        raise AzureAuthenticationError(msg) from error
    return EntraCredential(DefaultAzureCredential(), scope)
