"""The things this phase measures, behind one interface.

Three real systems and three deliberately broken ones, and the broken ones are
not a curiosity. A grader nobody has seen fail is a grader nobody should trust:
if a suite reports that everything scores well, the two explanations — the
systems are good, or the graders approve of anything — look identical from the
outside. So the suite runs an oracle that must score perfectly, a stand-in that
always refuses, and one that always answers without support, and asserts the
shape of each result (ADR-0046).

The three real ones differ in exactly the way the phase is asking about:

* ``answers`` retrieves once and generates once. No autonomy, bounded cost.
* ``incident-triage`` retrieves twice on two fixed framings, then generates. A
  deterministic graph — more retrieval, still no choice.
* ``investigator`` decides what to call and when to stop.
"""

from collections.abc import Mapping, Sequence

from paimon.application.use_cases import Answer, AnswerQuestion
from paimon.domain.entities import AgentRun, RunStatus
from paimon.domain.ports import AgentCheckpointer, AgentWorkflow, SearchFilters
from paimon.domain.value_objects import Citation
from paimon.evaluation.agent_dataset import AgentTask, Outcome
from paimon.evaluation.agent_grading import Attempt, Trajectory

#: Step detail keys the investigator records. Read defensively: the other agents
#: do not record them, and a missing key means "this system has no such notion"
#: rather than zero.
_STOP_REASON = "stop_reason"
_TOOL_CALLS = "tool_calls"
_TOOL_ERRORS = "tool_errors"
_REPEATED = "repeated_calls"


class AnsweringSystem:
    """Single-pass retrieval and generation — the platform without an agent.

    The control the whole phase rests on. Without it, "the agent scored 60%" has
    nothing to be 60% *of*, and a reader cannot tell whether the autonomy earned
    anything or whether the corpus was simply easy.
    """

    name = "answers"

    def __init__(self, answer: AnswerQuestion, tenant_id: str) -> None:
        """Wrap the answering use case."""
        self._answer = answer
        self._tenant_id = tenant_id

    async def attempt(self, task: AgentTask, trial: int) -> Attempt:
        """Answer one task once."""
        answer: Answer = await self._answer(task.question, SearchFilters(tenant_id=self._tenant_id))
        usage = answer.usage
        return Attempt(
            task_id=task.task_id,
            trial=trial,
            text=answer.text,
            citations=tuple(answer.citations),
            trajectory=Trajectory(
                # One retrieval and one generation, always. Recorded as a stop
                # reason so this system appears in the same histogram as the
                # others rather than as a gap in it.
                stop_reason="answered" if answer.grounded else "no_material",
                steps=("retrieve", "generate"),
                input_tokens=usage.input_tokens if usage else 0,
                output_tokens=usage.output_tokens if usage else 0,
            ),
        )


class WorkflowSystem:
    """Any registered agent, measured through the port the API uses.

    Deliberately not a private path into the graph. The benchmark starts a run
    and reads it back exactly as a caller does, so what is measured is what a
    caller would get — including the run record's citations, which is the whole
    reason they were put on it.
    """

    def __init__(
        self, workflow: AgentWorkflow, checkpointer: AgentCheckpointer, tenant_id: str
    ) -> None:
        """Wrap a compiled agent and the store its runs are recorded in."""
        self._workflow = workflow
        self._checkpointer = checkpointer
        self._tenant_id = tenant_id

    @property
    def name(self) -> str:
        """The agent's name, as its runs are recorded under."""
        return self._workflow.name

    async def attempt(self, task: AgentTask, trial: int) -> Attempt:
        """Run the agent once on one task."""
        thread_id = f"bench-{self.name}-{task.task_id}-{trial}"
        async for _ in self._workflow.stream(
            task.question, thread_id=thread_id, tenant_id=self._tenant_id
        ):
            pass

        run = await self._checkpointer.load(thread_id)
        if run is None:  # pragma: no cover - the workflow saves before it yields
            return Attempt(
                task_id=task.task_id,
                trial=trial,
                text="",
                failed=f"run '{thread_id}' was not recorded",
            )
        return _from_run(task, trial, run)


def _from_run(task: AgentTask, trial: int, run: AgentRun) -> Attempt:
    """Read an attempt out of a finished run record."""
    details = _last_details(run)
    return Attempt(
        task_id=task.task_id,
        trial=trial,
        text=run.answer,
        citations=tuple(run.citations),
        trajectory=Trajectory(
            stop_reason=details.get(_STOP_REASON, str(run.status)),
            tool_calls=_number(details.get(_TOOL_CALLS)),
            tool_errors=_number(details.get(_TOOL_ERRORS)),
            repeated_calls=_number(details.get(_REPEATED)),
            steps=tuple(step.name for step in run.steps),
            input_tokens=sum(step.input_tokens for step in run.steps),
            output_tokens=sum(step.output_tokens for step in run.steps),
        ),
        # A FAILED run is not a refusal. Recorded as a failure so an unreliable
        # system cannot score well on the tasks whose right answer is "I cannot".
        failed="the run failed" if run.status is RunStatus.FAILED else "",
    )


def _last_details(run: AgentRun) -> Mapping[str, str]:
    """The details of the last step that recorded a stop reason."""
    for step in reversed(tuple(run.steps)):
        if _STOP_REASON in step.details:
            return step.details
    return {}


def _number(raw: str | None) -> int:
    """Read a count a step recorded as text, treating absence as zero."""
    try:
        return int(raw) if raw is not None else 0
    except ValueError:  # pragma: no cover - the agent writes these itself
        return 0


class Oracle:
    """A system that always does exactly what the dataset asks for.

    Not a straw man — the opposite. It answers every answerable task by citing
    precisely the passages the task names, and refuses every task that should be
    refused. **It must score one on every grader**, and when it does not, the
    grader is wrong or the dataset is: an anchored quote that does not appear in
    the corpus, a refusal task that names supporting passages, a coverage rule
    that cannot be satisfied even in principle.

    That is the check Anthropic's guidance calls for and the one most easily
    skipped, because a suite where nothing fails looks finished.
    """

    name = "oracle"

    def __init__(self, documents: Mapping[str, str], refusal: str) -> None:
        """Build the oracle against the corpus it will cite.

        Args:
            documents: The corpus as indexed, so the citations it makes carry
                real offsets into real text rather than plausible ones.
            refusal: What this platform says when it cannot answer, so the
                oracle's refusals are graded by the same rule as everyone's.
        """
        self._documents = documents
        self._refusal = refusal

    async def attempt(self, task: AgentTask, trial: int) -> Attempt:
        """Produce the reference answer, or the reference refusal."""
        if task.expected is Outcome.REFUSE:
            return Attempt(task_id=task.task_id, trial=trial, text=self._refusal)

        citations = tuple(
            _cite(marker, passage.document_id, passage.quote, self._documents)
            for marker, passage in enumerate(task.supporting, start=1)
        )
        markers = " ".join(f"[{index}]" for index in range(1, len(citations) + 1))
        return Attempt(
            task_id=task.task_id,
            trial=trial,
            text=f"{task.reference} {markers}",
            citations=citations,
            trajectory=Trajectory(stop_reason="answered"),
        )


class AlwaysRefuses:
    """A system that never answers anything.

    Scores one on the refusal tasks and zero on everything else, which is the
    shape a grader must produce for it. A grader that rewarded it overall would
    be rewarding caution rather than correctness, and this platform's whole
    argument is that refusing *when the corpus is silent* is right and refusing
    when it is not is a failure like any other.
    """

    name = "always-refuses"

    def __init__(self, refusal: str) -> None:
        """Build it with the platform's own refusal sentence."""
        self._refusal = refusal

    async def attempt(self, task: AgentTask, trial: int) -> Attempt:
        """Refuse, whatever was asked."""
        return Attempt(task_id=task.task_id, trial=trial, text=self._refusal)


class AlwaysAnswersUncited:
    """A system that is fluent, confident and cites nothing.

    The failure this platform exists to prevent, as a measurable object. It must
    score **zero everywhere**, including on the refusal tasks — its output there
    is not a refusal, it is an invention, and a grader that could not tell those
    apart would report the worst possible system as the safest one.
    """

    name = "always-answers-uncited"

    async def attempt(self, task: AgentTask, trial: int) -> Attempt:
        """Assert something plausible, with nothing behind it."""
        return Attempt(
            task_id=task.task_id,
            trial=trial,
            text=(
                f"Based on our operational documentation, {task.question.rstrip('?')} "
                "is handled by the standard procedure, which applies in all cases."
            ),
            trajectory=Trajectory(stop_reason="answered"),
        )


def _cite(marker: int, document_id: str, quote: str, documents: Mapping[str, str]) -> Citation:
    """Build a citation pointing at where a quote really is in a document.

    Falls back to the whole document when the quote cannot be located, so a
    dataset error surfaces as an oracle that scores below one — which is a
    failing test — rather than as an exception three frames from the cause.
    """
    text = documents.get(document_id, "")
    start = _locate(text, quote)
    end = start + len(quote) if start >= 0 else max(len(text), 1)
    return Citation(
        marker=marker,
        document_id=document_id,
        chunk_id=f"{document_id}:oracle",
        source_uri=document_id,
        title=document_id,
        heading_path=(),
        start_char=max(start, 0),
        end_char=end,
        quote=text[max(start, 0) : end] if start >= 0 else quote,
    )


def _locate(text: str, quote: str) -> int:
    """Find a quote in a document, tolerating a rewrapped line break.

    The corpus is Markdown and a quotation spanning a wrap is stored with the
    newline the author typed. Matching on the exact string alone would miss it,
    and the benchmark's own ground truth normalizes whitespace for precisely
    this reason (ADR-0013).
    """
    direct = text.find(quote)
    if direct >= 0:
        return direct
    flattened = " ".join(quote.split())
    return text.find(flattened)


def refusal_systems(documents: Mapping[str, str], refusal: str) -> Sequence[object]:
    """The three stand-ins, for a suite that wants to watch its graders fail."""
    return (Oracle(documents, refusal), AlwaysRefuses(refusal), AlwaysAnswersUncited())


__all__ = [
    "AlwaysAnswersUncited",
    "AlwaysRefuses",
    "AnsweringSystem",
    "Oracle",
    "WorkflowSystem",
    "refusal_systems",
]
