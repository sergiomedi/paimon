"""Tests for the readiness use case."""

import asyncio

from paimon.application.use_cases import CheckReadiness


class StubProbe:
    """Probe with scripted behaviour."""

    def __init__(self, name: str, error: Exception | None = None, delay: float = 0.0) -> None:
        self._name = name
        self._error = error
        self._delay = delay

    @property
    def component(self) -> str:
        return self._name

    async def check(self) -> None:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error


class TestReporting:
    async def test_all_healthy_is_ready(self) -> None:
        report = await CheckReadiness([StubProbe("postgresql"), StubProbe("redis")])()
        assert report.is_ready
        assert [component.component for component in report.components] == ["postgresql", "redis"]

    async def test_one_failure_makes_the_instance_not_ready(self) -> None:
        report = await CheckReadiness(
            [StubProbe("postgresql"), StubProbe("redis", error=ConnectionError("refused"))]
        )()
        assert not report.is_ready

    async def test_a_failure_does_not_hide_the_other_components(self) -> None:
        """Knowing which dependency is down is the difference between a diagnosis
        and a restart."""
        report = await CheckReadiness(
            [StubProbe("postgresql", error=ConnectionError("refused")), StubProbe("redis")]
        )()
        postgres, redis = report.components
        assert postgres.healthy is False
        assert postgres.error == "ConnectionError: refused"
        assert redis.healthy is True
        assert redis.error is None

    async def test_no_probes_is_ready(self) -> None:
        report = await CheckReadiness([])()
        assert report.is_ready


class TestTimeouts:
    async def test_a_hanging_probe_is_reported_rather_than_awaited(self) -> None:
        """Without a timeout the orchestrator's own timeout fires and nothing says
        which dependency stalled."""
        report = await CheckReadiness([StubProbe("stuck", delay=5.0)], timeout_seconds=0.05)()
        (component,) = report.components
        assert component.healthy is False
        assert "timed out" in str(component.error)

    async def test_probes_run_concurrently(self) -> None:
        """Readiness latency must be the slowest probe, not the sum of them."""
        probes = [StubProbe(f"probe-{index}", delay=0.1) for index in range(5)]
        loop = asyncio.get_running_loop()
        started = loop.time()
        report = await CheckReadiness(probes, timeout_seconds=1.0)()
        elapsed = loop.time() - started

        assert report.is_ready
        assert elapsed < 0.3, f"probes appear to run serially: {elapsed:.2f}s for 5 x 0.1s"
