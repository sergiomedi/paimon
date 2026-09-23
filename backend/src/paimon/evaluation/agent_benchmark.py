"""Putting a dataset of open questions to a system, k times each, and scoring it.

Three things here that the answering benchmark does not do, each for a reason
this phase found rather than assumed.

**Every task is attempted k times.** An autonomous run is not reproducible even
at temperature zero: the model chooses what to call, and one different tool call
on turn two sends the rest of the run somewhere else. A single sample of that is
a number describing one afternoon, so the report carries pass@1, pass@k and
pass^k and the gap between the last two is the reliability (ADR-0046).

**The unit is the task, never the attempt.** Thirty tasks tried five times is
thirty observations. Treating it as a hundred and fifty would shrink every
interval by about the square root of five and claim a precision the experiment
does not have.

**Anything can be measured, not only an agent.** ``System`` asks for one method,
so the single-pass answering use case is measurable on the same dataset as the
two agents — which is what makes "was the autonomy worth it" a question with an
answer rather than a preference.
"""

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from inspect import isawaitable
from typing import Protocol

from paimon.evaluation.agent_dataset import AgentDataset, AgentTask, Outcome
from paimon.evaluation.agent_grading import Attempt, AttemptOutcome, Grade, Trajectory, grade
from paimon.evaluation.judging import Judgement, Verdict
from paimon.evaluation.progress import Progress
from paimon.evaluation.statistics import (
    Estimate,
    PairedDifference,
    Reliability,
    clustered_estimate,
    group_by_document,
    paired_difference,
    reliability,
)

#: How the benchmark asks for one refusal verdict. A callable rather than the
#: whole :class:`~paimon.evaluation.judging.RefusalJudge` protocol, because this
#: is all the benchmark needs and a narrower dependency is one a test can supply
#: in a line.
RefusalVerdict = Callable[[str, str], "Judgement | Awaitable[Judgement]"]


class System(Protocol):
    """Anything that can be asked an operational question.

    One method, deliberately narrow. An agent, a single-pass RAG answer and a
    deliberately broken stand-in all satisfy it, so all three are measurable on
    the same dataset by the same graders — and a comparison between them is a
    comparison rather than an analogy.
    """

    @property
    def name(self) -> str:
        """How this system appears in a report."""
        ...

    async def attempt(self, task: AgentTask, trial: int) -> Attempt:
        """Answer one task once.

        Implementations do not raise for an ordinary failure: a run that broke
        comes back as an :class:`~paimon.evaluation.agent_grading.Attempt` with
        ``failed`` set, so one crashed task does not end a two-hour benchmark.
        """
        ...


@dataclass(frozen=True, slots=True)
class TaskReport:
    """Every attempt at one task, and how each was graded."""

    task_id: str
    question: str
    category: str
    expected: Outcome
    attempts: tuple[Attempt, ...]
    grades: tuple[Grade, ...]

    @property
    def outcomes(self) -> tuple[bool, ...]:
        """Whether each attempt passed."""
        return tuple(item.passed for item in self.grades)

    @property
    def passes(self) -> int:
        """How many attempts passed."""
        return sum(self.outcomes)

    @property
    def rate(self) -> float:
        """The fraction of attempts that passed."""
        return self.passes / len(self.grades) if self.grades else 0.0

    @property
    def flaky(self) -> bool:
        """Whether the attempts disagreed with each other.

        The list worth reading first. A task that passes three times in five is
        not a task the system can do — it is a task the system can sometimes do,
        and an aggregate that reports 60% hides which of those it was.
        """
        return 0 < self.passes < len(self.grades)


@dataclass(frozen=True, slots=True)
class TrajectoryReport:
    """What the runs cost, and how they ended. Reported, never scored."""

    tool_calls: Estimate | None
    tool_errors: Estimate | None
    repeated_calls: Estimate | None
    total_tokens: Estimate
    latency_ms: Estimate
    stop_reasons: Mapping[str, int] = field(default_factory=dict)
    outcomes: Mapping[str, int] = field(default_factory=dict)
    failures: int = 0
    estimated_cost: float | None = None
    """What the tokens would cost at the configured prices. An estimate and
    labelled one (ADR-0028): the tokens are measured, the money is arithmetic
    over a price list that changes without telling anyone.

    None when this deployment prices nothing, or prices a different model from
    the one that ran. Not zero — zero is a claim, and the honest answer when
    nobody has supplied a price is silence."""


@dataclass(frozen=True, slots=True)
class CategoryReport:
    """One category's results, because one average over five hides all five."""

    category: str
    tasks: int
    reliability: Reliability


@dataclass(frozen=True, slots=True)
class AgentReport:
    """Everything one system's run over one dataset produced."""

    dataset: str
    system: str
    configuration: str
    trials: int
    reliability: Reliability
    categories: tuple[CategoryReport, ...]
    trajectory: TrajectoryReport
    tasks: tuple[TaskReport, ...]
    scores: Mapping[str, tuple[float, ...]] = field(default_factory=dict)
    clusters: tuple[str, ...] = ()
    judged: bool = False
    """Whether a model decided the outcomes. Every reliability number in a
    judged report is a judged number, and the report says so rather than
    presenting it beside the verified ones as though they were the same kind of
    thing (ADR-0030)."""

    @property
    def judge_disagreements(self) -> tuple[tuple[str, int], ...]:
        """Where the crude phrase probe read a response differently from the judge.

        Reported in full rather than resolved silently in favour of either. The
        probe is English-phrase matching against one model's idiom and the judge
        is a model's opinion; the cases they disagree about are the cases worth
        a person's eyes.
        """
        return tuple(
            (task.task_id, attempt.trial)
            for task in self.tasks
            for attempt, mark in zip(task.attempts, task.grades, strict=True)
            if mark.probe_agreed is False
        )

    @property
    def undecided(self) -> int:
        """Attempts the judge could not classify."""
        return sum(
            1
            for task in self.tasks
            for mark in task.grades
            if mark.judged and not mark.judge_reasoning
        )

    @property
    def flaky(self) -> tuple[TaskReport, ...]:
        """Tasks whose attempts disagreed with each other."""
        return tuple(task for task in self.tasks if task.flaky)

    @property
    def never_passed(self) -> tuple[TaskReport, ...]:
        """Tasks no attempt got right. The list to read before the aggregates."""
        return tuple(task for task in self.tasks if task.passes == 0)

    def compare(self, other: "AgentReport", metric: str = "pass_rate") -> PairedDifference:
        """Compare this system against another, task by task.

        Paired on the task, which is what makes a dataset this size able to say
        anything: two systems agree about which tasks are hard, and that
        correlation is most of the available information (ADR-0029).

        Raises:
            ValueError: If the runs are not comparable, or the metric is unknown.
        """
        if self.dataset != other.dataset:
            msg = f"different datasets: '{self.dataset}' and '{other.dataset}'"
            raise ValueError(msg)
        mine, theirs = self.scores.get(metric), other.scores.get(metric)
        if mine is None or theirs is None:
            available = ", ".join(sorted(self.scores)) or "none"
            msg = f"no per-task scores for '{metric}'; this run has: {available}"
            raise ValueError(msg)
        if len(mine) != len(theirs):
            msg = (
                f"'{self.system}' has {len(mine)} tasks and '{other.system}' has "
                f"{len(theirs)}; a paired comparison needs the same tasks"
            )
            raise ValueError(msg)
        return paired_difference(mine, theirs, self.clusters or None)


async def run_agent_benchmark(  # noqa: PLR0913  collaborators and a label, not flags
    dataset: AgentDataset,
    system: System,
    documents: Mapping[str, str],
    *,
    trials: int = 5,
    configuration: str = "unnamed",
    price: Callable[[int, int], float | None] | None = None,
    judge_refusal: "RefusalVerdict | None" = None,
    progress: Progress | None = None,
) -> AgentReport:
    """Put every task to a system k times and grade what comes back.

    Args:
        dataset: The golden set.
        system: What to measure.
        documents: The corpus as it was indexed, by document id, so citations
            can be followed into it.
        trials: Attempts per task. Five by default — enough for pass^k to mean
            something, few enough to finish against a local model.
        configuration: A label for what was measured. A number without the
            configuration that produced it cannot be compared with anything.
        judge_refusal: Decides whether a response declines to answer. Without
            it the outcome is read from citations and the platform's own refusal
            sentences alone, which cannot see a refusal a model wrote in its own
            words — so a run with no judge understates refusal and the report
            says which it was.
        price: Turns an attempt's input and output token counts into money, or
            returns None for a model nobody has priced. A callable rather than a
            price list, so the per-million arithmetic stays in the one place
            that already gets it right — converting at the call site is where a
            factor of a thousand goes unnoticed.
        progress: Notified after each task, when the caller wants to watch.

    Returns:
        The report, including the tasks no attempt got right.

    Raises:
        ValueError: If ``trials`` is not positive.
    """
    if trials < 1:
        msg = "a benchmark needs at least one attempt per task"
        raise ValueError(msg)

    reports: list[TaskReport] = []
    for task in dataset:
        attempts = [await _attempt_once(system, task, trial) for trial in range(1, trials + 1)]
        # The judge sees the question and the response and nothing else. Not
        # the category, not the expected outcome, not the task id: a judge told
        # that a refusal was expected is a judge told the answer.
        verdicts = [
            await _judge(judge_refusal, task.question, attempt.text) for attempt in attempts
        ]
        reports.append(
            TaskReport(
                task_id=task.task_id,
                question=task.question,
                category=task.category,
                expected=task.expected,
                attempts=tuple(attempts),
                grades=tuple(
                    grade(task, attempt, documents, verdict)
                    for attempt, verdict in zip(attempts, verdicts, strict=True)
                ),
            )
        )
        if progress is not None:
            progress(done=len(reports), total=len(dataset), case_id=task.task_id)

    clusters = _clusters(dataset)
    outcomes = [list(report.outcomes) for report in reports]
    return AgentReport(
        dataset=dataset.name,
        system=system.name,
        configuration=configuration,
        trials=trials,
        reliability=reliability(outcomes, clusters),
        categories=_by_category(dataset, reports),
        trajectory=_trajectory(reports, price),
        tasks=tuple(reports),
        scores={"pass_rate": tuple(report.rate for report in reports)},
        clusters=tuple(clusters),
        judged=judge_refusal is not None,
    )


async def regrade(  # noqa: PLR0913  collaborators and a label, not flags
    dataset: AgentDataset,
    stored: Mapping[str, Sequence[Attempt]],
    documents: Mapping[str, str],
    *,
    system: str,
    configuration: str,
    judge_refusal: "RefusalVerdict | None" = None,
    price: Callable[[int, int], float | None] | None = None,
    progress: Progress | None = None,
) -> AgentReport:
    """Score attempts that were recorded earlier, without running anything again.

    A grader change does not need a re-run. The transcripts carry the response
    text and its citations, which is everything the graders read, so a benchmark
    that took two hours can be re-scored in the time the judge takes — and the
    numbers before and after are comparable because they are the same attempts.

    That is not a convenience. The first grader in this phase read a refusal
    written in prose as an answer, and discovering it after 450 runs would have
    meant either re-running them or shipping the wrong number. Keeping the
    transcripts made it a re-score.

    Args:
        dataset: The golden set the attempts were made against.
        stored: The recorded attempts, by task id.
        documents: The corpus as indexed, for following citations.
        system: The name the attempts were produced by.
        configuration: The label of the run that produced them.
        judge_refusal: The judge, when re-grading with one.
        price: Turns token counts into money.
        progress: Notified per task.

    Returns:
        A report over the same attempts, graded afresh.

    Raises:
        ValueError: If the dataset and the stored attempts do not line up. A
            re-grade against a different dataset would score answers to
            questions nobody asked.
    """
    missing = [task.task_id for task in dataset if task.task_id not in stored]
    if missing:
        listed = ", ".join(missing[:5])
        msg = (
            f"{len(missing)} task(s) of '{dataset.name}' have no recorded attempts "
            f"({listed}); this report was produced from a different dataset"
        )
        raise ValueError(msg)

    reports: list[TaskReport] = []
    for task in dataset:
        attempts = tuple(stored[task.task_id])
        verdicts = [
            await _judge(judge_refusal, task.question, attempt.text) for attempt in attempts
        ]
        reports.append(
            TaskReport(
                task_id=task.task_id,
                question=task.question,
                category=task.category,
                expected=task.expected,
                attempts=attempts,
                grades=tuple(
                    grade(task, attempt, documents, verdict)
                    for attempt, verdict in zip(attempts, verdicts, strict=True)
                ),
            )
        )
        if progress is not None:
            progress(done=len(reports), total=len(dataset), case_id=task.task_id)

    clusters = _clusters(dataset)
    return AgentReport(
        dataset=dataset.name,
        system=system,
        configuration=configuration,
        trials=min((len(report.attempts) for report in reports), default=0),
        reliability=reliability([list(report.outcomes) for report in reports], clusters),
        categories=_by_category(dataset, reports),
        trajectory=_trajectory(reports, price),
        tasks=tuple(reports),
        scores={"pass_rate": tuple(report.rate for report in reports)},
        clusters=tuple(clusters),
        judged=judge_refusal is not None,
    )


async def _judge(judge: "RefusalVerdict | None", question: str, answer: str) -> Judgement | None:
    """Ask the judge to classify one response, surviving whatever it does.

    A judge that cannot be reached is not evidence about the response, so a
    failure becomes an abstention and the outcome falls back to what code can
    see — reported as undecided rather than as a refusal or an answer.
    """
    if judge is None:
        return None
    try:
        result = judge(question, answer)
        return await result if isawaitable(result) else result
    except Exception as error:  # noqa: BLE001  the judge is a model over a network
        return Judgement(
            verdict=Verdict.UNDECIDED,
            reasoning=f"the judge could not be reached: {error}",
            model_id="unreachable",
        )


async def _attempt_once(system: System, task: AgentTask, trial: int) -> Attempt:
    """Run one attempt, timing it and surviving whatever it does.

    A blind except, and the same argument the orchestration adapter makes for
    its own: this runs arbitrary adapter code against a model server, and
    anything it raises has to become a recorded failure rather than the end of a
    two-hour benchmark. The difference from the adapter is what happens next —
    the failure is graded as a failure, never as a refusal, so an unreliable
    system cannot score well on the tasks whose right answer is "I cannot".
    """
    started = time.perf_counter()
    try:
        attempt = await system.attempt(task, trial)
    except Exception as error:  # noqa: BLE001
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        return Attempt(
            task_id=task.task_id,
            trial=trial,
            text="",
            trajectory=Trajectory(latency_ms=elapsed, stop_reason="crashed"),
            failed=f"{type(error).__name__}: {error}",
        )
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    if attempt.trajectory.latency_ms:
        return attempt
    return replace(attempt, trajectory=replace(attempt.trajectory, latency_ms=elapsed))


def _clusters(dataset: AgentDataset) -> list[str]:
    """Label each task with the group it is correlated with.

    Tasks that draw on one document share its wording and whatever the chunker
    made of it, so they are not independent observations — the reasoning of
    ADR-0029, unchanged.

    The refusal tasks draw on **no** document, which would put every one of them
    in a single "unattributed" cluster and treat five unrelated questions as one
    observation. They are clustered by category instead: what they have in
    common is being out of corpus, which is the correlation that actually exists
    between them.
    """
    return [
        group_by_document([[passage.document_id for passage in task.supporting]])[0]
        if task.supporting
        else f"category:{task.category}"
        for task in dataset
    ]


def _by_category(
    dataset: AgentDataset, reports: Sequence[TaskReport]
) -> tuple[CategoryReport, ...]:
    """Summarise each category on its own.

    Unclustered inside a category, deliberately. Within one category the tasks
    are already the thing being varied, and borrowing the document clustering
    here would often leave a single group — which reports a standard error of
    zero and reads as certainty.
    """
    grouped: dict[str, list[list[bool]]] = {}
    for report in reports:
        grouped.setdefault(report.category, []).append(list(report.outcomes))
    return tuple(
        CategoryReport(category=name, tasks=len(outcomes), reliability=reliability(outcomes))
        for name, outcomes in ((name, grouped[name]) for name in dataset.categories)
    )


def _trajectory(
    reports: Sequence[TaskReport], price: "Callable[[int, int], float | None] | None"
) -> TrajectoryReport:
    """Aggregate what the runs cost and how they ended.

    Over **attempts**, not tasks, and that is the one place in this module where
    the attempt is the right unit: the question here is what a run costs, and
    every run cost something.
    """
    attempts = [attempt for report in reports for attempt in report.attempts]
    # Paired with the grades, because what an attempt *did* is the graded
    # outcome — which a judge may have decided — and not the code-only reading
    # on the attempt itself. Reporting the latter would say "answered" for every
    # prose refusal the judge had just recognised.
    graded = [mark for report in reports for mark in report.grades]
    if not attempts:
        nothing = Estimate(mean=0.0, standard_error=0.0, n=0)
        return TrajectoryReport(None, None, None, nothing, nothing)

    # Clustered by task: five attempts at one task are five samples of one
    # question's difficulty, not five independent observations of cost.
    tasks = [report.task_id for report in reports for _ in report.attempts]

    def over(values: list[float]) -> Estimate:
        return clustered_estimate(values, tasks)

    def counted(name: str) -> Estimate | None:
        """Aggregate a counter, or report that this system has no such counter.

        None when **no** attempt recorded it: the system does not do the thing.
        A partial absence is a different matter and is summed as zero, because
        an agent that made no tool calls on one task did make none.
        """
        values = [getattr(item.trajectory, name) for item in attempts]
        if all(value is None for value in values):
            return None
        return over([float(value or 0) for value in values])

    stop_reasons: dict[str, int] = {}
    outcomes: dict[str, int] = {}
    for attempt, mark in zip(attempts, graded, strict=True):
        if attempt.trajectory.stop_reason is not None:
            reason = attempt.trajectory.stop_reason or "unreported"
            stop_reasons[reason] = stop_reasons.get(reason, 0) + 1
        label = "crashed" if attempt.failed else mark.outcome.value
        outcomes[label] = outcomes.get(label, 0) + 1

    return TrajectoryReport(
        tool_calls=counted("tool_calls"),
        tool_errors=counted("tool_errors"),
        repeated_calls=counted("repeated_calls"),
        total_tokens=over([float(item.trajectory.total_tokens) for item in attempts]),
        latency_ms=over([item.trajectory.latency_ms for item in attempts]),
        stop_reasons=dict(sorted(stop_reasons.items())),
        outcomes=dict(sorted(outcomes.items())),
        failures=sum(1 for item in attempts if item.failed),
        estimated_cost=_cost(attempts, price),
    )


def _cost(
    attempts: Sequence[Attempt], price: "Callable[[int, int], float | None] | None"
) -> float | None:
    """What the tokens would have cost, when anyone has said what they cost.

    None rather than zero when the model is unpriced, and None rather than a
    partial total when *any* attempt is unpriced: a sum over the priced half of
    a run is a smaller number than the truth, presented with the authority of
    arithmetic.
    """
    if price is None:
        return None
    total = 0.0
    for attempt in attempts:
        each = price(attempt.trajectory.input_tokens, attempt.trajectory.output_tokens)
        if each is None:
            return None
        total += each
    return total


__all__ = [
    "AgentReport",
    "AttemptOutcome",
    "CategoryReport",
    "System",
    "TaskReport",
    "TrajectoryReport",
    "regrade",
    "run_agent_benchmark",
]
