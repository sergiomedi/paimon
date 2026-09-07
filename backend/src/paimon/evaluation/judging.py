"""Asking a model the questions arithmetic cannot answer.

ADR-0030 verified everything that could be verified: citations resolve, sentences
carry markers, quoted text is where it claims to be. Two questions survive that
and cannot be settled by a lookup — *is the sentence around a resolving citation
a fair reading of it*, and *does the answer address what was asked*. Those need a
judgement, so this asks for one, on terms chosen with the evidence about what
such judgements are worth (ADR-0031).

**The judge is defined here rather than in the domain, deliberately.** The
platform never judges anything at runtime; only the benchmark does. A port on the
domain would advertise a capability the product does not have, and would invite
somebody to reach for a model's opinion inside an answer path where a citation
would do.

Everything about the shape below is a mitigation for something measured:

* **Three discrete labels, not a score out of ten.** A judge asked for a number
  produces one with no stable meaning between runs; a judge asked to choose
  between three defined labels is answering a question.
* **Reasoning before the verdict**, and in that order in the JSON, so the label
  is written after the argument rather than justified afterwards.
* **Reference-guided.** The golden set already names the passage that answers each
  question, so the judge grades against a fixed anchor rather than its own idea
  of a good answer — consistently more reliable than prompt-only scoring.
* **Abstention is a valid outcome.** A judge that cannot decide says so and the
  case is excluded, rather than contributing a coin flip to an average.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class Verdict(StrEnum):
    """What a judge concluded about one answer.

    Three levels rather than a numeric scale, and the middle one exists because
    forcing a binary choice on a partially-supported answer pushes the judge
    towards the generous end — which is the direction it already errs.
    """

    YES = "yes"
    PARTIAL = "partial"
    NO = "no"
    UNDECIDED = "undecided"
    """The judge could not answer, or its answer could not be read. Excluded from
    the aggregate rather than counted as a failure: 'the judge broke' and 'the
    answer was bad' are different facts."""

    @property
    def score(self) -> float:
        """The verdict as a number, for averaging.

        ``UNDECIDED`` has no score. Callers filter it out before aggregating, and
        asking for one here raises rather than returning a default that would
        quietly become a data point.

        Raises:
            ValueError: If the verdict is ``UNDECIDED``.
        """
        if self is Verdict.UNDECIDED:
            msg = "an undecided verdict has no score; exclude it from the aggregate"
            raise ValueError(msg)
        return {Verdict.YES: 1.0, Verdict.PARTIAL: 0.5, Verdict.NO: 0.0}[self]


@dataclass(frozen=True, slots=True)
class Judgement:
    """One judged question.

    Attributes:
        verdict: What the judge concluded.
        reasoning: Why, in its words. Kept because a judged number nobody can
            audit is a number nobody should act on — and because reading these is
            how a rubric gets fixed.
        model_id: Which model judged. On every judgement, not only in the run's
            header: two runs judged by different models are not comparable, and
            the header is the first thing lost when numbers are copied out.
        unanimous: Whether repeated samples agreed. True when only one was taken,
            which is honest — a single sample cannot disagree with itself — and
            the sample count is recorded beside it.
        samples: How many times the judge was asked.
    """

    verdict: Verdict
    reasoning: str
    model_id: str
    unanimous: bool = True
    samples: int = 1


@dataclass(frozen=True, slots=True)
class JudgedAnswer:
    """Both judgements for one answer."""

    case_id: str
    faithfulness: Judgement
    relevance: Judgement


class AnswerJudge(Protocol):
    """Judges an answer against the passages it was meant to rest on."""

    @property
    def model_id(self) -> str:
        """Which model judges. Recorded on every judgement it produces."""
        ...

    async def judge_faithfulness(
        self, question: str, answer: str, references: Sequence[str]
    ) -> Judgement:
        """Decide whether the answer only says what the references support.

        Args:
            question: The question, as asked.
            answer: The answer to judge.
            references: The passages the golden set names for this question.

        Returns:
            The verdict and the reasoning behind it. Never raises for a bad
            answer: an answer the judge dislikes is a verdict, and an answer the
            judge cannot process is ``UNDECIDED``.
        """
        ...

    async def judge_relevance(self, question: str, answer: str) -> Judgement:
        """Decide whether the answer addresses the question that was asked.

        No references: relevance is a property of the question and the answer,
        and handing the judge the expected passages here would let it reward an
        answer for matching them rather than for answering.
        """
        ...


FAITHFULNESS_RUBRIC = """You are grading whether an answer stays within what its sources say.

You will be given a question, an answer, and the source passages the answer was \
supposed to rest on.

Decide, for the answer as a whole:

- "yes"     — every claim it makes is supported by the passages.
- "partial" — some claims are supported and at least one is not, or one is \
stated more strongly than the passages warrant.
- "no"      — its central claim is not supported by the passages.

Rules:

1. Judge only against the passages given. Do not use anything you know about the \
subject; an answer that is true in the world and absent from the passages is "no".
2. An answer that declines to answer, saying the sources do not cover the \
question, is "yes" — refusing is not an unsupported claim.
3. Length is not quality. A short answer that is fully supported is "yes"; a long \
one that adds an unsupported sentence is "partial".
4. Citation markers like [1] are formatting. Ignore them.

Reply with JSON only, in this order:

{"reasoning": "<one or two sentences>", "verdict": "yes" | "partial" | "no"}

Write the reasoning first and the verdict after it."""

RELEVANCE_RUBRIC = """You are grading whether an answer addresses the question asked.

Decide:

- "yes"     — it answers the question that was asked.
- "partial" — it addresses part of it, or answers a related question instead.
- "no"      — it does not address the question.

Rules:

1. Correctness is not your concern here, only whether it is an answer to *this* \
question. Something wrong but on-topic is "yes".
2. An answer that says the sources do not cover the question **is** addressing \
it: "yes".
3. Length is not quality.

Reply with JSON only, in this order:

{"reasoning": "<one or two sentences>", "verdict": "yes" | "partial" | "no"}

Write the reasoning first and the verdict after it."""


def render_references(references: Sequence[str]) -> str:
    """Lay out the reference passages for a judge to grade against."""
    if not references:
        return "(no passages were provided)"
    return "\n\n".join(f"[{index}] {text}" for index, text in enumerate(references, start=1))


def majority(verdicts: Sequence[Verdict]) -> tuple[Verdict, bool]:
    """Reduce repeated samples to one verdict, and say whether they agreed.

    A tie is ``UNDECIDED``. Breaking it towards the more generous label would
    bias the aggregate in exactly the direction judges already err; breaking it
    towards the harsher one is a different arbitrary rule. A judge that cannot
    make up its mind has said something worth recording.
    """
    usable = [verdict for verdict in verdicts if verdict is not Verdict.UNDECIDED]
    if not usable:
        return Verdict.UNDECIDED, True

    counts = {verdict: usable.count(verdict) for verdict in set(usable)}
    best = max(counts.values())
    winners = [verdict for verdict, count in counts.items() if count == best]
    if len(winners) > 1:
        return Verdict.UNDECIDED, False
    return winners[0], len(set(verdicts)) == 1


__all__ = [
    "FAITHFULNESS_RUBRIC",
    "RELEVANCE_RUBRIC",
    "AnswerJudge",
    "JudgedAnswer",
    "Judgement",
    "Verdict",
    "majority",
    "render_references",
]
