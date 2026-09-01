"""End-to-end tests for the health endpoints.

These exercise the assembled application — middleware, dependency wiring,
exception handlers — with the readiness probes replaced. The probes themselves
are covered by integration tests against real services.
"""

from fastapi import FastAPI
from httpx import AsyncClient

from paimon.application.use_cases import CheckReadiness
from paimon.interfaces.api.dependencies import get_check_readiness
from paimon.observability import CORRELATION_ID_HEADER


class StubProbe:
    def __init__(self, name: str, error: Exception | None = None) -> None:
        self._name = name
        self._error = error

    @property
    def component(self) -> str:
        return self._name

    async def check(self) -> None:
        if self._error:
            raise self._error


class TestLiveness:
    async def test_it_reports_alive(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "alive"}

    async def test_it_does_not_touch_dependencies(self, app: FastAPI, client: AsyncClient) -> None:
        """A liveness probe that fails on a database outage gets the container
        killed, which does not fix the database."""
        app.dependency_overrides[get_check_readiness] = lambda: CheckReadiness(
            [StubProbe("postgresql", error=ConnectionError("refused"))]
        )
        try:
            response = await client.get("/api/v1/health/live")
        finally:
            app.dependency_overrides.clear()
        assert response.status_code == 200


class TestReadiness:
    async def test_all_healthy_returns_200(self, app: FastAPI, client: AsyncClient) -> None:
        app.dependency_overrides[get_check_readiness] = lambda: CheckReadiness(
            [StubProbe("postgresql"), StubProbe("redis")]
        )
        try:
            response = await client.get("/api/v1/health/ready")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is True
        assert {component["component"] for component in body["components"]} == {
            "postgresql",
            "redis",
        }

    async def test_a_failure_returns_503_and_names_the_component(
        self, app: FastAPI, client: AsyncClient
    ) -> None:
        app.dependency_overrides[get_check_readiness] = lambda: CheckReadiness(
            [StubProbe("postgresql"), StubProbe("redis", error=ConnectionError("refused"))]
        )
        try:
            response = await client.get("/api/v1/health/ready")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 503
        body = response.json()
        assert body["ready"] is False
        failed = next(c for c in body["components"] if not c["healthy"])
        assert failed["component"] == "redis"
        assert "ConnectionError" in failed["error"]


class TestCorrelationId:
    async def test_a_generated_id_is_returned(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/health/live")
        assert response.headers[CORRELATION_ID_HEADER]

    async def test_an_inbound_id_is_echoed_back(self, client: AsyncClient) -> None:
        response = await client.get(
            "/api/v1/health/live", headers={CORRELATION_ID_HEADER: "caller-supplied"}
        )
        assert response.headers[CORRELATION_ID_HEADER] == "caller-supplied"
