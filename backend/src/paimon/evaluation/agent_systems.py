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
                # No stop reason and no tool calls, and neither is zero. This
                # system retrieves once and generates once; it does not choose
                # to stop and it does not call tools, so the honest entry is
                # that the question does not apply to it.
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
    # Absent, not zero. A fixed graph records none of these, and defaulting them
    # would report that incident-triage made no tool calls and stopped because
    # it "succeeded" — two statements in a vocabulary it does not have.
    return Attempt(
        task_id=task.task_id,
        trial=trial,
        text=run.answer,
        citations=tuple(run.citations),
        trajectory=Trajectory(
            stop_reason=details.get(_STOP_REASON),
            tool_calls=_number(details.get(_TOOL_CALLS)),
            tool_errors=_number(details.get(_TOOL_ERRORS)),
            repeated_calls=_number(details.get(_REPEATED)),
            steps=tuple(step.name for step in run.steps),
            # Every step's details, not just the last one's. The withdrawn draft
            # is recorded by `verify`, which is not always last, and the report
            # is the only durable copy — the run record lives in a table the
            # integration suite truncates.
            step_details=tuple(
                (step.name, dict(step.details)) for step in run.steps if step.details
            ),
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


def _number(raw: str | None) -> int | None:
    """Read a count a step recorded as text, treating absence as absence."""
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:  # pragma: no cover - the agent writes these itself
        return None


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
        )


#: A real refusal, copied from a measured run rather than invented. The `answers`
#: system produced it for a024 — "What is the escalation path for a security
#: incident involving customer data?" — and then cited the eight sources it had
#: just called irrelevant.
#:
#: It is here because the first grader scored this as a failure to refuse, on
#: all twenty-five such attempts, and none of the three stand-ins caught it:
#: every one of them refuses in the platform's canned words, so the grader was
#: only ever tested against refusals it was guaranteed to recognise.
REAL_PROSE_REFUSAL = (
    "The provided sources do not contain specific information about the escalation "
    "path for a security incident involving customer data. Therefore, I cannot "
    "provide an answer based on the given documentation.  [1][2][3][4][5][6][7][8]"
)


class RefusesInItsOwnWords:
    """A system that declines clearly, in prose, while citing what it read.

    The stand-in the first three were missing. It must be classified as a
    **refusal** — so it scores one on the out-of-corpus tasks and zero on the
    answerable ones, exactly like :class:`AlwaysRefuses` — even though it cites
    eight sources and matches none of the platform's canned refusal sentences.

    A grader that reads it as an answer is the grader this phase shipped first,
    and it reported a system refusing perfectly as refusing never.
    """

    name = "refuses-in-prose"

    def __init__(self, documents: Mapping[str, str]) -> None:
        """Build it against the corpus, so its citations resolve like any other."""
        self._documents = documents

    async def attempt(self, task: AgentTask, trial: int) -> Attempt:
        """Decline, in its own words, citing what it looked at."""
        cited = tuple(
            _cite(marker, document_id, text[:120], self._documents)
            for marker, (document_id, text) in enumerate(
                sorted(self._documents.items())[:8], start=1
            )
        )
        return Attempt(task_id=task.task_id, trial=trial, text=REAL_PROSE_REFUSAL, citations=cited)


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
    """The four stand-ins, for a suite that wants to watch its graders fail."""
    return (
        Oracle(documents, refusal),
        AlwaysRefuses(refusal),
        AlwaysAnswersUncited(),
        RefusesInItsOwnWords(documents),
    )


__all__ = [
    "REAL_PROSE_REFUSAL",
    "AlwaysAnswersUncited",
    "AlwaysRefuses",
    "AnsweringSystem",
    "Oracle",
    "RefusesInItsOwnWords",
    "WorkflowSystem",
    "refusal_systems",
]
