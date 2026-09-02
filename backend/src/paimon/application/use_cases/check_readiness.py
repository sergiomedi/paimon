"""Readiness: can this instance serve traffic right now."""

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass

from paimon.domain.ports import HealthProbe

DEFAULT_PROBE_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class ComponentStatus:
    """Outcome of a single probe."""

    component: str
    healthy: bool
    latency_ms: float
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """Outcome of every probe in one readiness check."""

    components: tuple[ComponentStatus, ...]

    @property
    def is_ready(self) -> bool:
        """Whether every component reported healthy."""
        return all(component.healthy for component in self.components)


class CheckReadiness:
    """Probe every dependency concurrently and report the results.

    Two properties matter more than they look.

    Probes run **concurrently**, so readiness latency is the slowest probe rather
    than the sum of all of them. An orchestrator polling this endpoint every few
    seconds will not tolerate a serial walk over a growing dependency list.

    Every probe is **bounded by a timeout**. A probe that hangs is worse than one
    that fails: the orchestrator's own timeout fires, the instance is reported
    unhealthy with no diagnosis, and nothing in the logs says which dependency
    stalled. A timeout turns that into a named failure.
    """

    def __init__(
        self,
        probes: Sequence[HealthProbe],
        timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    ) -> None:
        """Initialise the use case.

        Args:
            probes: One probe per dependency that must work for traffic to be served.
            timeout_seconds: Per-probe budget.
        """
        self._probes = tuple(probes)
        self._timeout_seconds = timeout_seconds

    async def __call__(self) -> ReadinessReport:
        """Run every probe and collect the results.

        Returns:
            A report covering every probe, in the order they were configured.
        """
        results = await asyncio.gather(*(self._run(probe) for probe in self._probes))
        return ReadinessReport(components=tuple(results))

    async def _run(self, probe: HealthProbe) -> ComponentStatus:
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._timeout_seconds):
                await probe.check()
        except TimeoutError:
            return ComponentStatus(
                component=probe.component,
                healthy=False,
                latency_ms=self._elapsed_ms(started),
                error=f"timed out after {self._timeout_seconds}s",
            )
        except Exception as error:  # noqa: BLE001  one bad probe must not hide the rest
            return ComponentStatus(
                component=probe.component,
                healthy=False,
                latency_ms=self._elapsed_ms(started),
                error=f"{type(error).__name__}: {error}",
            )
        return ComponentStatus(
            component=probe.component,
            healthy=True,
            latency_ms=self._elapsed_ms(started),
        )

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 3)
