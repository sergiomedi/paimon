"""End-to-end tests for the translation of domain errors into HTTP responses.

The exception handlers are the only place that knows both vocabularies: the
domain raises meaning, the interface decides status codes. These tests pin that
mapping.
"""

from fastapi import FastAPI
from httpx import AsyncClient

from paimon.domain.entities import Principal
from paimon.domain.errors import DomainError, IdentityProviderUnavailableError
from paimon.interfaces.api.dependencies import get_identity_provider


class RaisingIdentityProvider:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def authenticate(self, token: str) -> Principal:
        raise self._error


class TestErrorMapping:
    async def test_an_unreachable_provider_is_503_not_401(
        self, app: FastAPI, client: AsyncClient
    ) -> None:
        """We cannot tell whether the token is valid, so we must not tell the
        client their credentials are bad."""
        app.dependency_overrides[get_identity_provider] = lambda: RaisingIdentityProvider(
            IdentityProviderUnavailableError("keys unreachable")
        )
        try:
            response = await client.get("/api/v1/me", headers={"Authorization": "Bearer anything"})
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 503
        assert response.json()["detail"] == "identity provider unavailable"

    async def test_an_unexpected_domain_error_is_500_without_leaking_detail(
        self, app: FastAPI, client: AsyncClient
    ) -> None:
        app.dependency_overrides[get_identity_provider] = lambda: RaisingIdentityProvider(
            DomainError("a message that must not reach the client")
        )
        try:
            response = await client.get("/api/v1/me", headers={"Authorization": "Bearer anything"})
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 500
        assert response.json()["detail"] == "internal error"

    async def test_every_error_carries_the_correlation_id(
        self, app: FastAPI, client: AsyncClient
    ) -> None:
        """Without it, a user reporting an error gives support nothing to search for."""
        app.dependency_overrides[get_identity_provider] = lambda: RaisingIdentityProvider(
            IdentityProviderUnavailableError("keys unreachable")
        )
        try:
            response = await client.get("/api/v1/me", headers={"Authorization": "Bearer anything"})
        finally:
            app.dependency_overrides.clear()

        assert response.json()["correlation_id"] == response.headers["X-Correlation-ID"]
