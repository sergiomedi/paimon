"""Running the agent benchmark, and rendering what it found.

Kept beside the retrieval command rather than inside it. The two share a corpus,
a tenant and a report file, and nothing else: this one puts open questions to a
whole system several times and scores the outcome, where that one puts fifteen
questions to a retriever once and scores the ranking. Folding them into one
``main`` would have produced a function whose every branch asked which benchmark
it was running.
"""

import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from paimon.application.use_cases import AnswerQuestion, RetrieveChunks
from paimon.domain.ports import AgentCheckpointer, AgentWorkflow, SearchFilters
from paimon.domain.value_objects import Citation
from paimon.evaluation import (
    AgentDataset,
    AgentReport,
    AnsweringSystem,
    Oracle,
    System,
    WorkflowSystem,
)
from paimon.evaluation.agent_benchmark import TaskReport, TrajectoryReport
from paimon.evaluation.agent_dataset import Outcome
from paimon.evaluation.agent_grading import Attempt, Trajectory
from paimon.evaluation.statistics import Estimate, Reliability

#: Systems that exist to check the graders rather than to be measured. Named so
#: ``--system oracle`` works without the caller knowing it is not an agent.
STANDINS = ("oracle",)


@dataclass(frozen=True, slots=True)
class Bench:
    """What a benchmark run needs in order to build any system it is asked for.

    Named rather than passed as seven arguments, the way
    :class:`~paimon.agents.collaborators.AgentCollaborators` was: the linter
    counts, and a list that long is usually a concept nobody has named. Here the
    concept is real — every one of these is fixed for the whole run, and a
    system built with a different tenant or a different corpus would not be
    comparable with the others.
    """

    workflows: Mapping[str, AgentWorkflow]
    answerer: AnswerQuestion
    checkpointer: AgentCheckpointer
    documents: Mapping[str, str]
    tenant_id: str
    refusal: str

    def system(self, name: str) -> System:
        """Resolve a system by name, or say what names exist.

        Raises:
            ValueError: If nothing goes by that name. The message lists what
                does, because the commonest cause is an agent this deployment
                cannot run — the investigator on a model without tool calling —
                and "no system named x" alone reads as a typo.
        """
        if name == AnsweringSystem.name:
            return AnsweringSystem(self.answerer, self.tenant_id)
        if name == Oracle.name:
            return Oracle(self.documents, self.refusal)
        workflow = self.workflows.get(name)
        if workflow is not None:
            return WorkflowSystem(workflow, self.checkpointer, self.tenant_id)
        offered = ", ".join([*sorted(self.workflows), AnsweringSystem.name, *STANDINS])
        msg = f"no system named '{name}'; this deployment offers: {offered}"
        raise ValueError(msg)


async def verify_corpus(
    dataset: AgentDataset, retrieve: RetrieveChunks, tenant_id: str
) -> str | None:
    """Check the corpus is actually retrievable before measuring against it.

    One search, for a passage the dataset itself says exists. It costs an
    embedding and a query, and it turns the worst failure this harness can have
    into a refusal.

    That failure is not hypothetical. A benchmark run against an empty index
    does not crash: every search returns nothing, every task ends in
    ``no_material``, the out-of-corpus tasks score 100% because refusing is
    right for them, and the report is a complete, confidently formatted
    measurement of nothing. It is indistinguishable at a glance from a model
    that simply cannot use its tools — which is exactly the conclusion this
    phase exists to reach or reject, so it must not be reachable by accident.

    Returns:
        What is wrong and what to do about it, or None when the corpus answers.
    """
    probe = next(
        (passage for item in dataset for passage in item.supporting),
        None,
    )
    if probe is None:  # pragma: no cover - a set of only refusals
        return None

    result = await retrieve(probe.quote, SearchFilters(tenant_id=tenant_id))
    if result.hits:
        return None
    return (
        f"the corpus is not retrievable as tenant '{tenant_id}'.\n\n"
        f"Searching for a passage the dataset says is in '{probe.document_id}' returned\n"
        "nothing, so every task would end in no_material and the report would be a\n"
        "confidently formatted measurement of an empty index.\n\n"
        "Ingest the corpus for this tenant, and check that nothing else truncated it:\n"
        "the integration tests TRUNCATE chunks and documents, so running them against\n"
        "the same database during a benchmark empties it mid-run."
    )


def render(report: AgentReport) -> str:
    """Render an agent report for a terminal.

    Three sections, in the order they should be read. The reliability first,
    because pass@1 and pass^k are different claims and quoting one as the other
    is the mistake this format exists to make hard. Then the categories, because
    one average over five of them hides all five. Then the trajectory, which is
    what the run cost and is never part of the score.
    """
    stats = report.reliability
    lines = [
        "",
        f"dataset       {report.dataset}  ({stats.tasks} tasks, "
        f"{stats.pass_at_1.clusters or stats.tasks} independent groups)",
        f"system        {report.system}",
        f"configuration {report.configuration}",
        f"trials        {stats.trials} per task",
        "",
        "  reliability          mean +/- 95% CI      what it says",
        f"  pass@1         {stats.pass_at_1.format(percent=True):>18}   one run gets it right",
        *_repeated(stats),
        "",
        "  by category        tasks     pass@1            pass^k",
    ]
    for category in report.categories:
        lines.append(
            f"  {category.category:<18}{category.tasks:>5}   "
            f"{category.reliability.pass_at_1.format(percent=True):>16}  "
            f"{category.reliability.pass_hat_k.format(percent=True):>16}"
        )

    trajectory = report.trajectory
    lines.extend(
        [
            "",
            "  trajectory (reported, never scored)",
            f"    tool calls per run    {_or_na(trajectory.tool_calls)}",
            f"    tool errors per run   {_or_na(trajectory.tool_errors)}",
            f"    repeated calls        {_or_na(trajectory.repeated_calls)}",
            f"    tokens per run        {trajectory.total_tokens.format()}",
            f"    latency per run       {trajectory.latency_ms.mean / 1000:.2f} s",
        ]
    )
    if trajectory.estimated_cost is not None:
        lines.append(
            f"    estimated cost        {trajectory.estimated_cost:.4f} (an estimate, not a bill)"
        )
    lines.append(f"    stop reasons          {_histogram(trajectory.stop_reasons)}")
    lines.append(f"    what it produced      {_histogram(trajectory.outcomes)}")
    if trajectory.failures:
        lines.append(f"    runs that crashed     {trajectory.failures}")

    if report.flaky:
        lines.extend(["", "  answered inconsistently across trials:"])
        lines.extend(
            f"    {task.task_id}  {task.passes}/{len(task.grades)}  {task.question[:62]}"
            for task in report.flaky
        )
    if report.never_passed:
        lines.extend(["", "  never got right:"])
        lines.extend(
            f"    {task.task_id}  [{task.category}]  {task.question[:58]}"
            for task in report.never_passed
        )
    lines.append("")
    return "\n".join(lines)


def _repeated(stats: Reliability) -> list[str]:
    """The two rates that only mean something above one trial.

    At k=1 they are arithmetically equal to pass@1 and printing them would be
    three names for one number — which is precisely the misreading this format
    exists to prevent, since the whole point of pass^k is that it differs.
    """
    if stats.trials < 2:  # noqa: PLR2004  one trial is not a repetition
        return [
            "",
            "  pass@k and pass^k need more than one trial; this run made one,",
            "  so it reports how often it succeeded and nothing about how reliably.",
        ]
    return [
        f"  pass@{stats.trials:<9}{stats.pass_at_k.format(percent=True):>18}   "
        "at least one of the runs does",
        f"  pass^{stats.trials:<9}{stats.pass_hat_k.format(percent=True):>18}   "
        "every run does — what it is worth unwatched",
        "",
        "  The unit is the task, not the attempt: intervals are over tasks, so",
        f"  {stats.tasks} tasks tried {stats.trials} times is {stats.tasks} observations,",
        f"  not {stats.tasks * stats.trials}.",
    ]


def _or_na(value: Estimate | None) -> str:
    """Render a counter, or say this system has no such counter.

    "n/a" and not "0.000". Zero is a measurement — it says the system made no
    tool calls — and printing it for a system that cannot make one puts a
    finding in a column where there is only an absence.
    """
    return value.format() if value is not None else "n/a  (this system has none)"


def _histogram(counts: Mapping[str, int]) -> str:
    """Render a small tally inline, or say there is nothing to tally."""
    if not counts:
        return "n/a  (this system does not stop for a reason)"
    return ", ".join(f"{name} {count}" for name, count in counts.items())


def render_comparison(report: AgentReport, baseline: AgentReport) -> str:
    """Compare two systems task by task.

    Paired on the task, which is what lets a set this size say anything: the
    systems agree about which tasks are hard, and that correlation is most of
    the information available (ADR-0029).
    """
    difference = report.compare(baseline)
    low, high = difference.interval()
    verdict = (
        "distinguishable from zero"
        if difference.is_significant()
        else "not distinguishable from noise"
    )
    lines = [
        "",
        f"  {report.system} against {baseline.system}, task by task",
        "",
        f"    difference in pass rate   {difference.mean:+.1%}  [{low:+.1%}, {high:+.1%}]",
        f"    p                         {difference.p_value:.3f}   {verdict}",
        f"    agreement about difficulty r={difference.correlation:.2f}",
        "",
    ]
    lines.extend(_where_they_differ(report, baseline))
    lines.append("")
    return "\n".join(lines)


def _where_they_differ(report: AgentReport, baseline: AgentReport) -> list[str]:
    """List the tasks the two systems disagreed about, and which way.

    The part worth reading. An aggregate that moved says something changed;
    these say what, and a phase that concluded "autonomy helps" without them
    would be quoting a number nobody had looked behind.

    Read from the per-task scores rather than from the grades, because a
    baseline loaded from a file has scores and no grades — and a comparison that
    silently reported every baseline task as a zero would invent a landslide.
    """
    mine = report.scores.get("pass_rate", ())
    theirs = baseline.scores.get("pass_rate", ())
    if len(mine) != len(theirs) or len(mine) != len(report.tasks):
        return ["    the two runs do not line up task for task"]

    better: list[str] = []
    worse: list[str] = []
    for task, ours, yours in zip(report.tasks, mine, theirs, strict=True):
        if ours == yours:
            continue
        line = f"      {task.task_id}  [{task.category}]  {ours:.0%} against {yours:.0%}"
        (better if ours > yours else worse).append(line)

    lines: list[str] = []
    for label, entries in (("won", better), ("lost", worse)):
        if entries:
            lines.append(f"    {label} on {len(entries)}:")
            lines.extend(entries)
    return lines or ["    no task separated them"]


def write_report(report: AgentReport, path: Path) -> None:
    """Write the full report, transcripts included.

    The transcripts are the point of keeping it. Anthropic's guidance on agent
    evals is blunt that the aggregate is where you stop looking and the
    transcripts are where you find out why, so a report that dropped them would
    be a report nobody could act on.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(report), indent=2, default=str), encoding="utf-8")


class FileJournal:
    """A journal on disk, one attempt per line, appended as it completes.

    JSON Lines and append-only, because the failure it exists for is the process
    dying: a file rewritten wholesale is a file that can be truncated mid-write,
    and a run that lost its record to the crash it was protecting against would
    be worse than no journal at all.

    A line that cannot be parsed is skipped rather than fatal. That is exactly
    the half-written last line a kill leaves behind, and refusing to start
    because of it would turn a recoverable interruption into a lost run.
    """

    def __init__(self, path: Path) -> None:
        """Open, or resume, the journal at this path."""
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def completed(self) -> dict[tuple[str, int], Attempt]:
        """Every attempt already recorded, by task and trial."""
        if not self._path.exists():
            return {}
        found: dict[tuple[str, int], Attempt] = {}
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                attempt = _attempt_from(json.loads(line))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            found[(attempt.task_id, attempt.trial)] = attempt
        return found

    def record(self, attempt: Attempt) -> None:
        """Append one finished attempt, and flush it before returning."""
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(attempt), ensure_ascii=False, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def read_attempts(path: Path) -> tuple[str, str, dict[str, list[Attempt]]]:
    """Read the recorded attempts out of a report, for re-grading.

    Returns:
        The system's name, the configuration label, and every attempt by task
        id — everything a grader reads, which is why a grader change needs no
        re-run.

    Raises:
        ValueError: If the file carries no transcripts. An older report written
            without them can be compared against and cannot be re-scored, and
            saying so is better than silently re-grading nothing.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    tasks = raw.get("tasks")
    if not tasks or "attempts" not in tasks[0]:
        msg = (
            f"'{path}' carries no transcripts, so it cannot be re-graded. "
            "Re-run the benchmark to produce one."
        )
        raise ValueError(msg)

    stored: dict[str, list[Attempt]] = {}
    for entry in tasks:
        stored[str(entry["task_id"])] = [_attempt_from(item) for item in entry["attempts"]]
    return str(raw.get("system", "unknown")), str(raw.get("configuration", "unnamed")), stored


def _attempt_from(raw: Mapping[str, Any]) -> Attempt:
    """Rebuild one recorded attempt."""
    trajectory = raw.get("trajectory") or {}
    return Attempt(
        task_id=str(raw["task_id"]),
        trial=int(raw["trial"]),
        text=str(raw["text"]),
        citations=tuple(_citation_from(item) for item in raw.get("citations", ())),
        trajectory=Trajectory(
            stop_reason=trajectory.get("stop_reason"),
            tool_calls=trajectory.get("tool_calls"),
            tool_errors=trajectory.get("tool_errors"),
            repeated_calls=trajectory.get("repeated_calls"),
            steps=tuple(trajectory.get("steps", ())),
            # Carried through a regrade, or a re-scored report would silently
            # lose the withdrawn drafts it was written to keep.
            step_details=tuple(
                (str(name), dict(details)) for name, details in trajectory.get("step_details", ())
            ),
            input_tokens=int(trajectory.get("input_tokens", 0)),
            output_tokens=int(trajectory.get("output_tokens", 0)),
            latency_ms=float(trajectory.get("latency_ms", 0.0)),
        ),
        failed=str(raw.get("failed", "")),
    )


def _citation_from(raw: Mapping[str, Any]) -> Citation:
    """Rebuild one recorded citation, offsets and all."""
    return Citation(
        marker=int(raw["marker"]),
        document_id=str(raw["document_id"]),
        chunk_id=str(raw["chunk_id"]),
        source_uri=str(raw["source_uri"]),
        title=str(raw["title"]),
        heading_path=tuple(raw.get("heading_path", ())),
        start_char=int(raw["start_char"]),
        end_char=int(raw["end_char"]),
        quote=str(raw["quote"]),
    )


def load_report(path: Path) -> AgentReport:
    """Read a report written by an earlier run, for comparison.

    Only what a paired comparison needs is rebuilt. Reconstructing the whole
    object would mean a second parser to keep in step with the dataclasses, for
    information a comparison does not use.

    Raises:
        ValueError: If the file carries no per-task scores.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    scores = raw.get("scores")
    if not scores:
        msg = (
            f"'{path}' carries no per-task scores, so it cannot be compared task "
            "by task. Re-run the baseline to produce one."
        )
        raise ValueError(msg)
    nothing = Estimate(mean=0.0, standard_error=0.0, n=0)
    return AgentReport(
        dataset=raw["dataset"],
        system=raw["system"],
        configuration=raw.get("configuration", "unnamed"),
        trials=int(raw.get("trials", 1)),
        reliability=Reliability(
            tasks=len(next(iter(scores.values()), ())),
            trials=int(raw.get("trials", 1)),
            pass_at_1=nothing,
            pass_at_k=nothing,
            pass_hat_k=nothing,
        ),
        categories=(),
        trajectory=_no_trajectory(),
        tasks=_task_stubs(raw.get("tasks", ())),
        scores={name: tuple(values) for name, values in scores.items()},
        clusters=tuple(raw.get("clusters", ())),
    )


def _no_trajectory() -> TrajectoryReport:
    """A trajectory with nothing in it, for a report read back for comparison."""
    nothing = Estimate(mean=0.0, standard_error=0.0, n=0)
    return TrajectoryReport(nothing, nothing, nothing, nothing, nothing)


def _task_stubs(raw: Sequence[Mapping[str, object]]) -> tuple[TaskReport, ...]:
    """Enough of each task to say which ones the two systems disagreed about."""
    return tuple(
        TaskReport(
            task_id=str(item["task_id"]),
            question=str(item.get("question", "")),
            category=str(item.get("category", "")),
            expected=Outcome(str(item.get("expected", "answer"))),
            attempts=(),
            grades=(),
        )
        for item in raw
    )


def load_dataset(path: Path) -> AgentDataset:
    """Load the agent golden set, failing loudly on a malformed one."""
    return AgentDataset.from_jsonl(path)


def emit(rendered: str) -> None:
    """Print a rendered report to stdout."""
    sys.stdout.write(rendered)


__all__ = [
    "STANDINS",
    "Bench",
    "FileJournal",
    "emit",
    "load_dataset",
    "load_report",
    "read_attempts",
    "render",
    "render_comparison",
    "verify_corpus",
    "write_report",
]
