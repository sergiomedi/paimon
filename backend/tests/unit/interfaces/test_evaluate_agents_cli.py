"""The agent benchmark's command surface, and the one refusal that matters most.

A benchmark run against an empty index does not crash. Every search returns
nothing, every task ends in ``no_material``, the out-of-corpus tasks score 100%
because refusing is right for them, and the result is a complete, confidently
formatted measurement of nothing — indistinguishable at a glance from a model
that cannot use its tools. Since that is precisely the conclusion this phase
exists to reach or reject, it must not be reachable by accident.
"""

import json
from pathlib import Path

import pytest
from tests.unit.evaluation.test_agent_grading import DOCS, TASKS

from paimon.domain.entities import Chunk
from paimon.domain.ports import SearchFilters
from paimon.evaluation import run_agent_benchmark
from paimon.evaluation.agent_dataset import AgentDataset
from paimon.evaluation.agent_grading import Attempt
from paimon.evaluation.agent_systems import AlwaysRefuses
from paimon.interfaces.cli.evaluate_agents import (
    Bench,
    FileJournal,
    load_report,
    render,
    render_comparison,
    verify_corpus,
    write_report,
)

ROOT = Path(__file__).resolve().parents[4]
DATASET = AgentDataset.from_jsonl(ROOT / "evaluation" / "datasets" / "agents-v1.jsonl")
TENANT = "bench"


class _Result:
    def __init__(self, hits: tuple[object, ...]) -> None:
        self.hits = hits


class _Hit:
    def __init__(self, chunk: Chunk) -> None:
        self.chunk = chunk


class FakeRetrieve:
    """Retrieval that finds something, or does not, on request."""

    def __init__(self, *, finds: bool) -> None:
        self._finds = finds
        self.queries: list[str] = []

    async def __call__(self, query: str, filters: SearchFilters) -> _Result:
        self.queries.append(query)
        if not self._finds:
            return _Result(())
        chunk = Chunk(
            chunk_id="c1",
            document_id="node-maintenance",
            tenant_id=filters.tenant_id,
            ordinal=0,
            text="Nodes are drained before any kernel upgrade, without exception.",
            start_char=0,
            end_char=62,
            token_count=10,
        )
        return _Result((_Hit(chunk),))


class TestRefusingToMeasureNothing:
    async def test_an_unreachable_corpus_stops_the_run(self) -> None:
        refusal = await verify_corpus(DATASET, FakeRetrieve(finds=False), TENANT)  # type: ignore[arg-type]

        assert refusal is not None
        assert "not retrievable" in refusal

    async def test_the_refusal_names_the_tenant_and_a_document(self) -> None:
        # Both halves are what somebody needs to find the mistake: the wrong
        # tenant is the commonest cause, and the document says which search was
        # tried.
        refusal = await verify_corpus(DATASET, FakeRetrieve(finds=False), TENANT)  # type: ignore[arg-type]

        assert refusal is not None
        assert TENANT in refusal
        assert any(item.supporting[0].document_id in refusal for item in DATASET if item.supporting)

    async def test_it_warns_about_the_thing_that_actually_happened(self) -> None:
        # The integration tests TRUNCATE chunks and documents. Running them
        # against the same database during a benchmark empties it mid-run, which
        # is how the first measured run of this phase produced garbage.
        refusal = await verify_corpus(DATASET, FakeRetrieve(finds=False), TENANT)  # type: ignore[arg-type]

        assert refusal is not None
        assert "TRUNCATE" in refusal

    async def test_a_reachable_corpus_passes(self) -> None:
        assert await verify_corpus(DATASET, FakeRetrieve(finds=True), TENANT) is None  # type: ignore[arg-type]

    async def test_it_probes_with_a_passage_the_dataset_names(self) -> None:
        # Not an arbitrary string. Searching for "test" would find something in
        # most corpora and prove nothing about this one.
        retrieve = FakeRetrieve(finds=True)

        await verify_corpus(DATASET, retrieve, TENANT)  # type: ignore[arg-type]

        expected = next(item.supporting[0].quote for item in DATASET if item.supporting)
        assert retrieve.queries == [expected]

    async def test_it_costs_exactly_one_search(self) -> None:
        # A guard that re-ran the whole dataset would cost as much as the thing
        # it guards.
        retrieve = FakeRetrieve(finds=True)

        await verify_corpus(DATASET, retrieve, TENANT)  # type: ignore[arg-type]

        assert len(retrieve.queries) == 1


class TestChoosingASystem:
    @staticmethod
    def bench(**overrides: object) -> Bench:
        values: dict[str, object] = {
            "workflows": {"investigator": object(), "incident-triage": object()},
            "answerer": object(),
            "checkpointer": object(),
            "documents": {"node-maintenance": "text"},
            "tenant_id": TENANT,
            "refusal": "I have no indexed material.",
        }
        values.update(overrides)
        return Bench(**values)  # type: ignore[arg-type]

    def test_an_unknown_system_lists_what_exists(self) -> None:
        # The commonest cause is an agent this deployment cannot run — the
        # investigator on a model without tool calling — and "no system named x"
        # alone reads as a typo.
        with pytest.raises(ValueError, match="incident-triage, investigator, answers, oracle"):
            self.bench().system("investigatorr")

    def test_the_single_pass_path_is_selectable(self) -> None:
        assert self.bench().system("answers").name == "answers"

    def test_the_oracle_is_selectable(self) -> None:
        assert self.bench().system("oracle").name == "oracle"

    def test_a_registered_agent_is_selectable(self) -> None:
        workflow = self.bench().system("investigator")
        assert workflow is not None


class TestTheReportFile:
    async def test_a_report_round_trips_enough_to_compare(self, tmp_path: Path) -> None:
        # A baseline read back must carry the per-task scores, or the comparison
        # silently reports every baseline task as a zero and invents a landslide.

        report = await run_agent_benchmark(
            TASKS, AlwaysRefuses("no"), DOCS, trials=1, configuration="x"
        )
        path = tmp_path / "report.json"
        write_report(report, path)

        loaded = load_report(path)

        assert loaded.scores["pass_rate"] == report.scores["pass_rate"]
        assert loaded.system == report.system

    def test_a_report_without_scores_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "old.json"
        path.write_text('{"dataset": "d", "system": "s"}', encoding="utf-8")

        with pytest.raises(ValueError, match="carries no per-task scores"):
            load_report(path)

    async def test_the_transcripts_are_kept(self, tmp_path: Path) -> None:
        # The aggregate is where you stop looking and the transcripts are where
        # you find out why, so a report that dropped them could not be acted on.

        report = await run_agent_benchmark(TASKS, AlwaysRefuses("no"), DOCS, trials=1)
        path = tmp_path / "report.json"
        write_report(report, path)

        raw = json.loads(path.read_text(encoding="utf-8"))

        assert raw["tasks"][0]["attempts"][0]["text"]


class TestRendering:
    async def test_one_trial_says_it_measured_no_reliability(self) -> None:

        report = await run_agent_benchmark(TASKS, AlwaysRefuses("no"), DOCS, trials=1)

        rendered = render(report)

        assert "need more than one trial" in rendered
        assert "pass^1" not in rendered

    async def test_more_than_one_trial_reports_both_rates(self) -> None:

        report = await run_agent_benchmark(TASKS, AlwaysRefuses("no"), DOCS, trials=3)

        rendered = render(report)

        assert "pass@3" in rendered
        assert "pass^3" in rendered

    async def test_a_comparison_of_a_system_with_itself_separates_nothing(self) -> None:

        report = await run_agent_benchmark(TASKS, AlwaysRefuses("no"), DOCS, trials=1)

        rendered = render_comparison(report, report)

        assert "no task separated them" in rendered


class TestResumingAfterADeath:
    """A run of several hours that keeps nothing until the end loses everything.

    Three runs died before this existed — twice to a machine short of memory,
    once to a session ending — and each time the work was gone. The journal is
    append-only JSON Lines: a file rewritten wholesale can be truncated
    mid-write, and a record lost to the crash it was protecting against would be
    worse than no record.
    """

    @staticmethod
    def _attempt(task_id: str, trial: int, text: str = "an answer", failed: str = "") -> Attempt:
        return Attempt(task_id=task_id, trial=trial, text=text, failed=failed)

    def test_an_empty_journal_has_nothing_to_resume(self, tmp_path: Path) -> None:
        assert FileJournal(tmp_path / "j.jsonl").completed() == {}

    def test_a_recorded_attempt_comes_back(self, tmp_path: Path) -> None:
        journal = FileJournal(tmp_path / "j.jsonl")

        journal.record(self._attempt("a001", 1, "Cordon the node first [1]."))

        found = FileJournal(tmp_path / "j.jsonl").completed()
        assert found[("a001", 1)].text == "Cordon the node first [1]."

    def test_it_is_keyed_by_task_and_trial(self, tmp_path: Path) -> None:
        # Five trials of one task are five attempts, not one.
        journal = FileJournal(tmp_path / "j.jsonl")

        for trial in (1, 2, 3):
            journal.record(self._attempt("a001", trial))

        assert set(FileJournal(tmp_path / "j.jsonl").completed()) == {
            ("a001", 1),
            ("a001", 2),
            ("a001", 3),
        }

    def test_a_half_written_last_line_does_not_stop_a_resume(self, tmp_path: Path) -> None:
        # Exactly what a kill leaves behind. Refusing to start because of it
        # would turn a recoverable interruption into a lost run.
        path = tmp_path / "j.jsonl"
        journal = FileJournal(path)
        journal.record(self._attempt("a001", 1))
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"task_id": "a002", "tri')

        assert set(FileJournal(path).completed()) == {("a001", 1)}

    async def test_a_resumed_run_does_not_run_what_is_recorded(self) -> None:
        counted = CountingSystem()
        journal = ListJournal(
            [Attempt(task_id=task.task_id, trial=1, text="done") for task in TASKS]
        )

        await run_agent_benchmark(TASKS, counted, DOCS, trials=1, journal=journal)

        assert counted.calls == 0

    async def test_it_runs_only_what_is_missing(self) -> None:
        counted = CountingSystem()
        half = [t.task_id for t in TASKS][: len(TASKS) // 2]
        journal = ListJournal([Attempt(task_id=t, trial=1, text="done") for t in half])

        await run_agent_benchmark(TASKS, counted, DOCS, trials=1, journal=journal)

        assert counted.calls == len(TASKS) - len(half)

    async def test_a_crashed_attempt_is_not_reused(self) -> None:
        # It may have failed because the machine was dying rather than because
        # the system did. Carrying a harness failure into a resumed run would
        # bake an accident into the numbers.
        counted = CountingSystem()
        journal = ListJournal(
            [Attempt(task_id=t.task_id, trial=1, text="", failed="killed") for t in TASKS]
        )

        await run_agent_benchmark(TASKS, counted, DOCS, trials=1, journal=journal)

        assert counted.calls == len(TASKS)

    async def test_what_it_runs_is_written_down(self) -> None:
        counted = CountingSystem()
        journal = ListJournal([])

        await run_agent_benchmark(TASKS, counted, DOCS, trials=2, journal=journal)

        assert len(journal.written) == len(TASKS) * 2


class CountingSystem:
    """A system that answers instantly and counts how often it was asked."""

    name = "counting"

    def __init__(self) -> None:
        self.calls = 0

    async def attempt(self, task: object, trial: int) -> Attempt:
        self.calls += 1
        return Attempt(task_id=task.task_id, trial=trial, text="fresh")  # type: ignore[attr-defined]


class ListJournal:
    """A journal in memory, so the runner's resume logic is testable without a disk."""

    def __init__(self, existing: list[Attempt]) -> None:
        self._done = {(a.task_id, a.trial): a for a in existing}
        self.written: list[Attempt] = []

    def completed(self) -> dict[tuple[str, int], Attempt]:
        return dict(self._done)

    def record(self, attempt: Attempt) -> None:
        self.written.append(attempt)
