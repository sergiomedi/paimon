"""End-to-end tests for the authenticated endpoint.

The value of /me is that it exercises the whole authentication path: header
parsing, adapter, claim mapping and domain entity. A wiring mistake surfaces
here rather than in the first feature that depends on it.
"""

from datetime import timedelta

from httpx import AsyncClient

from paimon.infrastructure.identity import DevIdentityProvider


class TestAuthenticated:
    async def test_it_returns_the_caller(
        self, client: AsyncClient, dev_identity_provider: DevIdentityProvider
    ) -> None:
        token = dev_identity_provider.issue(
            subject="user-1",
            tenant_id="tenant-1",
            display_name="Ada Lovelace",
            roles=frozenset({"reader"}),
        )
        response = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 200
        assert response.json() == {
            "subject": "user-1",
            "tenant_id": "tenant-1",
            "display_name": "Ada Lovelace",
            "roles": ["reader"],
        }


class TestUnauthenticated:
    async def test_no_header_is_401(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/me")
        assert response.status_code == 401

    async def test_a_non_bearer_scheme_is_401(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/me", headers={"Authorization": "Basic abc"})
        assert response.status_code == 401

    async def test_an_expired_token_is_401(
        self, client: AsyncClient, dev_identity_provider: DevIdentityProvider
    ) -> None:
        token = dev_identity_provider.issue(
            subject="user-1", tenant_id="tenant-1", expires_in=timedelta(seconds=-60)
        )
        response = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401

    async def test_the_reason_is_not_disclosed(
        self, client: AsyncClient, dev_identity_provider: DevIdentityProvider
    ) -> None:
        """Telling a caller why a token was rejected helps them forge a better one."""
        expired = dev_identity_provider.issue(
            subject="user-1", tenant_id="tenant-1", expires_in=timedelta(seconds=-60)
        )
        responses = [
            await client.get("/api/v1/me"),
            await client.get("/api/v1/me", headers={"Authorization": "Bearer garbage"}),
            await client.get("/api/v1/me", headers={"Authorization": f"Bearer {expired}"}),
        ]
        details = {response.json()["detail"] for response in responses}
        assert details == {"invalid or missing token"}

    async def test_the_error_carries_the_correlation_id(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/me")
        assert response.json()["correlation_id"]
