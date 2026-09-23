"""Grading what an agent produced, by looking rather than by asking.

The order is the one ADR-0030 established for answers and ADR-0046 restates for
agents: verify everything that can be verified, judge only what cannot, and say
in the report which is which. Three of the four things worth knowing about an
agent's output are lookups, not opinions:

* **Did it reach the right outcome** — answer when the corpus supports one,
  refuse when it does not. Decided from the citations, not from the prose.
* **Do its citations cover what the task said the answer rests on** — a quote
  containment check against the golden passages.
* **Do its citations survive being followed** — the attribution verifier already
  written for Phase 6, opening each document at the offsets claimed.

**The route is not graded.** How many tools a run called, in what order, with
what queries, is reported beside the score and never inside it. Two runs that
reach the same cited answer by different routes are the same result; scoring the
route would be scoring the author's idea of how the problem should be solved,
which is the thing an autonomous agent exists not to be told.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from paimon.agents import investigator, triage
from paimon.application.use_cases.answer_question import NO_MATERIAL
from paimon.domain.value_objects import Citation
from paimon.evaluation.agent_dataset import AgentTask, Outcome
from paimon.evaluation.attribution import AttributionReport, check_answer

#: Every sentence this platform says instead of an answer. Collected from the
#: modules that define them rather than restated, so a reworded refusal cannot
#: silently start being graded as a confident answer.
#:
#: Matching is by equality against this set, not by looking for words like
#: "cannot". A keyword rule would classify *"the runbook says you cannot raise
#: the ceiling"* — a correct, cited answer — as a refusal.
REFUSALS: frozenset[str] = frozenset(
    {
        NO_MATERIAL,
        triage.UNSUPPORTED,
        triage.RETRIEVAL_FAILED,
        investigator.UNSUPPORTED,
        investigator.RETRIEVAL_FAILED,
        *investigator.REFUSALS.values(),
    }
)


class AttemptOutcome(StrEnum):
    """What a system actually did, as opposed to what it was asked to do.

    Three values, and the third is the one that makes the grading honest. A
    system that produces confident prose with nothing behind it has neither
    answered nor refused, and collapsing it into either would hide the failure
    this platform exists to prevent: on an answerable task it would look like a
    cautious refusal, and on an unanswerable one it would look like success.
    """

    ANSWERED = "answered"
    """It cited something. Whether the citations hold up is a separate question."""

    REFUSED = "refused"
    """It said, in the platform's own words, that it could not answer."""

    UNGROUNDED = "ungrounded"
    """It made claims and cited nothing. Never a pass, for either kind of task."""


@dataclass(frozen=True, slots=True)
class Trajectory:
    """How a run got where it got. Reported, never scored.

    Every field here is a cost or a symptom rather than a quality. Tool calls
    and tokens are what autonomy is paid for; tool errors and repeated calls are
    how a run wastes them; the stop reason is how it ended. A benchmark that
    folded any of these into the score would be rewarding systems for being
    cheap rather than for being right — and the interesting question this phase
    asks is exactly what the extra cost buys.
    """

    stop_reason: str = ""
    tool_calls: int = 0
    tool_errors: int = 0
    repeated_calls: int = 0
    steps: tuple[str, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0

    @property
    def total_tokens(self) -> int:
        """What the attempt cost in tokens."""
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class Attempt:
    """One system's output on one task, once.

    One *attempt*, not one result, because a task is put to a system several
    times: an autonomous run is not reproducible even at temperature zero, and a
    single sample of an unreliable process is a number that describes one
    afternoon (ADR-0046).
    """

    task_id: str
    trial: int
    text: str
    citations: tuple[Citation, ...] = ()
    trajectory: Trajectory = field(default_factory=Trajectory)
    failed: str = ""
    """Why the run did not complete, when it did not. A crashed run is not a
    refusal and is not an answer; it is scored as a failure and reported apart,
    because "the agent is wrong" and "the harness broke" are different facts."""

    @property
    def outcome(self) -> AttemptOutcome:
        """What this attempt did, by the platform's own rules."""
        if self.citations:
            return AttemptOutcome.ANSWERED
        if self.text.strip() in REFUSALS:
            return AttemptOutcome.REFUSED
        return AttemptOutcome.UNGROUNDED


@dataclass(frozen=True, slots=True)
class Grade:
    """What the code graders made of one attempt.

    Attributes:
        outcome_correct: Whether it answered when it should have, or refused
            when it should have. The first thing to look at, because every other
            number is conditional on it.
        support_coverage: Of the passages the task says an answer rests on, the
            fraction some citation actually covers. One for a refusal task, by
            convention: there is nothing to cover and reporting zero would drag
            the average down for doing the right thing.
        citation_precision: Of the citations made, the fraction that survive
            being opened at the offsets they claim.
        attribution: The full per-citation report behind that number, kept so a
            disagreement can be settled by reading rather than by re-running.
    """

    outcome_correct: bool
    support_coverage: float
    citation_precision: float
    attribution: AttributionReport | None = None

    @property
    def passed(self) -> bool:
        """Whether this attempt counts as a success.

        Strict on purpose, and the strictness is the same rule the platform
        makes to its users: an answer is right when it is right *and* checkable.
        Covering three of four supporting passages is a partially answered
        question, and a citation that does not resolve is the system doing the
        one thing it exists not to do.
        """
        return (
            self.outcome_correct and self.support_coverage == 1.0 and self.citation_precision == 1.0
        )


def support_coverage(task: AgentTask, citations: Sequence[Citation]) -> float:
    """What fraction of the expected passages the citations actually cover.

    A passage counts as covered when some citation names its document *and*
    quotes text containing it. Both halves are needed: citing the right document
    at the wrong offsets is how an answer points at a real source that does not
    say what the answer claims, which is indistinguishable from a good citation
    to anyone who does not open it.

    Returns:
        One when there is nothing to cover, so a refusal task is not penalised.
    """
    if not task.supporting:
        return 1.0
    covered = sum(
        1
        for passage in task.supporting
        if any(passage.is_supported_by(cited.document_id, cited.quote) for cited in citations)
    )
    return covered / len(task.supporting)


def grade(task: AgentTask, attempt: Attempt, documents: Mapping[str, str]) -> Grade:
    """Grade one attempt against one task, with no model involved.

    Args:
        task: What was asked, and what a right outcome is.
        attempt: What came back.
        documents: The corpus as it was indexed, by document id. The normalized
            text a parser produced, not the file on disk: a citation's offsets
            are into the former, and checking against the latter would report
            every citation as broken for every document the parser touched.

    Returns:
        The three code-grader results, and the evidence for the third.
    """
    if attempt.failed:
        # A run that crashed answered nothing and refused nothing. Grading it as
        # a refusal would let an unreliable system score well on the tasks whose
        # right answer is "I cannot".
        return Grade(outcome_correct=False, support_coverage=0.0, citation_precision=0.0)

    outcome = attempt.outcome
    if task.expected is Outcome.REFUSE:
        correct = outcome is AttemptOutcome.REFUSED
        return Grade(
            outcome_correct=correct,
            support_coverage=1.0,
            # Nothing was cited, so there is nothing to have got wrong. Reporting
            # zero here would punish a correct refusal on a metric about
            # citations it rightly did not make.
            citation_precision=1.0 if correct else 0.0,
        )

    if outcome is not AttemptOutcome.ANSWERED:
        return Grade(outcome_correct=False, support_coverage=0.0, citation_precision=0.0)

    attribution = check_answer(attempt.text, attempt.citations, documents)
    return Grade(
        outcome_correct=True,
        support_coverage=support_coverage(task, attempt.citations),
        citation_precision=attribution.citation_accuracy,
        attribution=attribution,
    )


__all__ = [
    "REFUSALS",
    "Attempt",
    "AttemptOutcome",
    "Grade",
    "Trajectory",
    "grade",
    "support_coverage",
]
