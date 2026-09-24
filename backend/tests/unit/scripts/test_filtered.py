"""Attributing a failure to a task, and refusing to when it cannot be done.

`scripts/azure/filtered.py` decides which tasks leave a paired comparison. It
reads a task id off the benchmark's next progress line and attributes the
requests above it to that task — which is sound only while one task runs at a
time. If the benchmark ever runs them concurrently the blocks still *look* well
formed, because the progress lines are still in order, and every attribution is
quietly wrong.

That matters because of what the attribution is for. A corroborated failure is
excluded from a comparison; an uncorroborated one is counted against the system.
Attributing another task's HTTP 400 to this one excludes a task on somebody
else's evidence, which is worse than excluding nothing at all.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

MODULE = Path(__file__).resolve().parents[4] / "scripts" / "azure" / "filtered.py"


def load() -> ModuleType:
    """Import the script by path, since scripts/ is not a package.

    Making it importable would mean changing the thing under test to suit the
    test, which is the same trade `test_deployment_errors.py` makes.
    """
    specification = importlib.util.spec_from_file_location("filtered", MODULE)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def request(when: str, status: str = '"HTTP/1.1 200 OK"') -> str:
    """One httpx log line, shaped the way the benchmark's logger writes it."""
    return (
        f"{when} [info     ] HTTP Request: POST "
        f"https://oai.example.com/openai/deployments/chat/chat/completions"
        f"?api-version=2024-10-21 {status} [httpx] service=paimon-api"
    )


def refused(when: str) -> str:
    """A content-filter rejection, as httpx logs it."""
    return request(when, '"HTTP/1.1 400 Bad Request"')


def progress(system: str, index: int, task: str) -> str:
    """The line the benchmark prints when a task finishes."""
    return f"{system} x3 {index}/30  {task}"


class TestAttributingAFailureToATask:
    def test_requests_belong_to_the_task_whose_line_follows_them(self, tmp_path: Path) -> None:
        log = tmp_path / "run.log"
        log.write_text(
            "\n".join(
                [
                    refused("2026-09-24T17:00:01.000000Z"),
                    refused("2026-09-24T17:00:02.000000Z"),
                    progress("answers", 1, "a028"),
                    request("2026-09-24T17:00:05.000000Z"),
                    progress("answers", 2, "a029"),
                ]
            ),
            encoding="utf-8",
        )

        found = load().evidence_from_log(log)

        assert sorted(found) == [("answers", "a028")]
        assert len(found[("answers", "a028")]) == 2

    def test_a_task_with_no_refusals_is_absent(self, tmp_path: Path) -> None:
        # Absent rather than empty: a task nothing went wrong for must not
        # appear in a report about things that went wrong.
        log = tmp_path / "run.log"
        log.write_text(
            "\n".join(
                [
                    request("2026-09-24T17:00:01.000000Z"),
                    progress("answers", 1, "a001"),
                ]
            ),
            encoding="utf-8",
        )

        assert load().evidence_from_log(log) == {}


class TestRefusingToAttribute:
    """Two tasks running at once, which is the case position cannot survive."""

    def test_interleaved_tasks_are_refused_rather_than_guessed_at(self, tmp_path: Path) -> None:
        # a028's block ends at 17:00:10; a029's first request is at 17:00:05,
        # before that. The progress lines are still in order, so only the clock
        # reveals that the two tasks overlapped.
        module = load()
        log = tmp_path / "run.log"
        log.write_text(
            "\n".join(
                [
                    refused("2026-09-24T17:00:01.000000Z"),
                    request("2026-09-24T17:00:10.000000Z"),
                    progress("answers", 1, "a028"),
                    request("2026-09-24T17:00:05.000000Z"),
                    request("2026-09-24T17:00:12.000000Z"),
                    progress("answers", 2, "a029"),
                ]
            ),
            encoding="utf-8",
        )

        with pytest.raises(module.InterleavedTasks) as refusal:
            module.evidence_from_log(log)

        message = str(refusal.value)
        assert "overlap in time" in message
        assert "answers/a029" in message, "the refusal must name the tasks involved"
        assert "counts as a system failure" in message, "it must say what follows"

    def test_sequential_tasks_are_not_refused(self, tmp_path: Path) -> None:
        # The guard must not fire on the ordinary case, or it would refuse
        # every real log and silently exclude nothing forever.
        module = load()
        log = tmp_path / "run.log"
        log.write_text(
            "\n".join(
                [
                    refused("2026-09-24T17:00:01.000000Z"),
                    request("2026-09-24T17:00:02.000000Z"),
                    progress("answers", 1, "a028"),
                    request("2026-09-24T17:00:03.000000Z"),
                    request("2026-09-24T17:00:04.000000Z"),
                    progress("answers", 2, "a029"),
                ]
            ),
            encoding="utf-8",
        )

        assert sorted(module.evidence_from_log(log)) == [("answers", "a028")]

    def test_touching_blocks_are_allowed(self, tmp_path: Path) -> None:
        # One task's last request and the next one's first sharing a timestamp
        # is a clock with one-second resolution, not concurrency.
        module = load()
        log = tmp_path / "run.log"
        log.write_text(
            "\n".join(
                [
                    refused("2026-09-24T17:00:01.000000Z"),
                    progress("answers", 1, "a028"),
                    request("2026-09-24T17:00:01.000000Z"),
                    progress("answers", 2, "a029"),
                ]
            ),
            encoding="utf-8",
        )

        assert sorted(module.evidence_from_log(log)) == [("answers", "a028")]
