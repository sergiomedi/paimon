"""Runs and steps: the invariants that make a trace trustworthy."""

from datetime import UTC, datetime, timedelta

import pytest

from paimon.domain.entities import AgentRun, AgentStep, RunStatus

BEGAN = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)


def step(name: str = "retrieve", **overrides: object) -> AgentStep:
    values: dict[str, object] = {
        "name": name,
        "summary": "ran",
        "started_at": BEGAN,
        "finished_at": BEGAN + timedelta(milliseconds=250),
    }
    values.update(overrides)
    return AgentStep(**values)  # type: ignore[arg-type]


class TestAgentStep:
    def test_it_reports_how_long_the_node_took(self) -> None:
        assert step().duration_ms == pytest.approx(250.0)

    def test_it_sums_the_tokens_the_node_spent(self) -> None:
        assert step(input_tokens=90, output_tokens=10).total_tokens == 100

    def test_an_unnamed_step_cannot_be_attributed(self) -> None:
        with pytest.raises(ValueError, match="name the node"):
            step(name="  ")

    def test_a_naive_timestamp_is_refused(self) -> None:
        # A trace ordered by naive timestamps is a trace that reorders itself
        # when the deployment moves timezone.
        with pytest.raises(ValueError, match="timezone-aware"):
            step(started_at=datetime(2026, 9, 3, 9, 0))  # noqa: DTZ001

    def test_a_step_cannot_finish_before_it_starts(self) -> None:
        with pytest.raises(ValueError, match="cannot finish before"):
            step(finished_at=BEGAN - timedelta(seconds=1))

    def test_negative_token_counts_are_refused(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            step(input_tokens=-1)


class TestAgentRun:
    def test_it_totals_what_the_run_has_cost(self) -> None:
        run = AgentRun(
            thread_id="t-1",
            agent="triage",
            tenant_id="tenant-a",
            status=RunStatus.RUNNING,
            steps=(step(input_tokens=40), step("answer", output_tokens=60)),
        )
        assert run.total_tokens == 100

    @pytest.mark.parametrize(
        ("status", "terminal"),
        [
            (RunStatus.RUNNING, False),
            (RunStatus.AWAITING_INPUT, False),
            (RunStatus.SUCCEEDED, True),
            (RunStatus.FAILED, True),
        ],
    )
    def test_only_a_finished_run_is_terminal(self, status: RunStatus, *, terminal: bool) -> None:
        # Awaiting input is deliberately not terminal: the run has not ended, it
        # is waiting for a person, and a poller that treats it as finished will
        # never come back for the decision.
        run = AgentRun(thread_id="t-1", agent="triage", tenant_id="tenant-a", status=status)
        assert run.is_terminal is terminal

    @pytest.mark.parametrize("blank", ["thread_id", "agent", "tenant_id"])
    def test_a_run_that_cannot_be_addressed_is_refused(self, blank: str) -> None:
        values = {
            "thread_id": "t-1",
            "agent": "triage",
            "tenant_id": "tenant-a",
            "status": RunStatus.RUNNING,
        }
        values[blank] = "  "
        with pytest.raises(ValueError, match=f"non-empty {blank}"):
            AgentRun(**values)  # type: ignore[arg-type]
