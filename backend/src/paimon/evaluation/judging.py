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
* **Reference-guided, and each question against its own reference.** A judge
  grading against a fixed anchor is consistently more reliable than one asked for
  its own idea of a good answer. What differs is *which* anchor, and getting that
  wrong is how this platform spent a phase reporting a number that measured
  nothing anybody wanted (ADR-0033): faithfulness is graded against **the sources
  the model was shown**, completeness against **the passage the golden set
  names**, and relevance against neither.
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
    """Every judgement for one answer.

    Faithfulness and completeness are the precision and recall of the same
    answer and they are not redundant: an answer can invent nothing and omit
    everything, or repeat the golden passage and bury it in fabrication. One
    number cannot say which happened, and the version of this platform that
    tried scored a well-behaved system at 0.667 for elaborating correctly.
    """

    case_id: str
    faithfulness: Judgement
    completeness: Judgement
    relevance: Judgement


class AnswerJudge(Protocol):
    """Judges an answer against the passages it was meant to rest on."""

    @property
    def model_id(self) -> str:
        """Which model judges. Recorded on every judgement it produces."""
        ...

    async def judge_faithfulness(
        self, question: str, answer: str, sources: Sequence[str]
    ) -> Judgement:
        """Decide whether the answer only says what its sources support.

        Args:
            question: The question, as asked.
            answer: The answer to judge.
            sources: The numbered sources the answer was generated from, in
                marker order. **Not** the golden passage: the golden set names
                one sentence, chosen to score retrieval, and an answer that
                correctly elaborates from the rest of the corpus is a good
                answer that grading against that sentence calls unfaithful.

        Returns:
            The verdict and the reasoning behind it. Never raises for a bad
            answer: an answer the judge dislikes is a verdict, and an answer the
            judge cannot process is ``UNDECIDED``.
        """
        ...

    async def judge_completeness(
        self, question: str, answer: str, references: Sequence[str]
    ) -> Judgement:
        """Decide whether the answer carries what the golden passages say.

        The recall half, and the one question the golden set is the right anchor
        for: it was written to name what answers each question.

        Args:
            question: The question, as asked.
            answer: The answer to judge.
            references: The passages the golden set names for this question.

        Returns:
            The verdict and the reasoning behind it.
        """
        ...

    async def judge_relevance(self, question: str, answer: str) -> Judgement:
        """Decide whether the answer addresses the question that was asked.

        No references: relevance is a property of the question and the answer,
        and handing the judge the expected passages here would let it reward an
        answer for matching them rather than for answering.
        """
        ...


FAITHFULNESS_RUBRIC = """You are grading whether an answer stays inside the sources it was given.

You will be given a question, the numbered sources whoever wrote the answer was \
shown, and the answer.

Decide, for the answer as a whole:

- "yes"     — every claim it makes is supported by the numbered sources.
- "partial" — some claims are supported and at least one is not, or one is \
stated more strongly than the sources warrant.
- "no"      — its central claim is not supported by the numbered sources.

Rules:

1. Judge only against the numbered sources. Do not use anything you know about \
the subject; a claim that is true in the world and absent from the sources is \
unsupported.
2. The sources are the whole of the evidence, not a summary of it. A claim \
supported anywhere in them is supported, whether or not the answer drew \
attention to that source.
3. An answer that declines to answer, saying the sources do not cover the \
question, is "yes" — refusing is not an unsupported claim.
4. Length is not quality, and detail is not invention. A long answer that \
elaborates from the sources is "yes"; a long one that adds a sentence the \
sources do not carry is "partial".
5. A marker like [1] refers to the source of that number. A claim carrying the \
wrong marker is still supported if the sources support it — whether a citation \
points where it claims is checked by opening it, not by asking you.

Reply with JSON only, in this order:

{"reasoning": "<one or two sentences>", "verdict": "yes" | "partial" | "no"}

Write the reasoning first and the verdict after it."""

COMPLETENESS_RUBRIC = """You are grading whether an answer carries what its reference passages say.

You will be given a question, the reference passages a golden set names as \
answering it, and the answer.

Decide:

- "yes"     — the answer conveys what the reference passages say about the question.
- "partial" — it conveys some of that and leaves out something they answer.
- "no"      — it conveys none of it.

Rules:

1. Wording does not have to match. The question is whether a reader of the \
answer learns what the passages say, not whether they were quoted.
2. Extra material is not your concern here. An answer that says everything the \
passages say **and more** is "yes"; whether the extra is supported is graded \
separately.
3. An answer that declines to answer is "no" when the passages do answer the \
question. Refusing is an honest failure and it is still a failure to convey them.
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
    "COMPLETENESS_RUBRIC",
    "FAITHFULNESS_RUBRIC",
    "RELEVANCE_RUBRIC",
    "AnswerJudge",
    "JudgedAnswer",
    "Judgement",
    "Verdict",
    "majority",
    "render_references",
]
