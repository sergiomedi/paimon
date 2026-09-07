"""A judge backed by a chat model.

It lives here rather than in ``infrastructure`` and needed no exception to the
layering contracts to do so, which is the tell that it is in the right place: it
depends on the :class:`~paimon.domain.ports.ChatModel` **port**, not on a
provider. Whichever adapter the composition root hands it — a local endpoint,
Azure OpenAI — is somebody else's decision. The first attempt did put it in
``infrastructure``, and ``import-linter`` refused it for importing the rubric;
the fix was to move the file rather than to widen the rule.

This is the part that talks to a model and turns what comes back into a
verdict. Three things it does that a thinner wrapper would not:

**It parses strictly and abstains rather than guessing.** A reply that is not the
JSON the rubric asked for becomes ``UNDECIDED``, not a default label. A judge
whose malformed output silently becomes "yes" is worse than no judge, because the
failure looks like a passing grade.

**It can sample more than once.** Judgements have variance; asking three times and
taking the majority costs three calls and reports whether the samples agreed. One
sample is the default, because most runs do not need it and every run pays for it.

**It records which model judged, on every judgement.** Two runs judged by
different models are not comparable, and a run header is the first thing lost when
numbers are copied into a table.
"""

import json
import re
from collections.abc import Sequence

from paimon.domain.errors import GenerationError
from paimon.domain.ports import ChatModel, Message
from paimon.evaluation.judging import (
    FAITHFULNESS_RUBRIC,
    RELEVANCE_RUBRIC,
    Judgement,
    Verdict,
    majority,
    render_references,
)

#: Models wrap JSON in prose or a fenced block often enough that refusing those
#: replies would throw away usable verdicts. The object is extracted; anything
#: that is still not JSON is an abstention.
JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

#: Judging is a classification, not a composition. Temperature zero so a rerun of
#: the same benchmark produces the same numbers, which is the difference between
#: a measurement and an anecdote.
TEMPERATURE = 0.0


class ModelAnswerJudge:
    """Judges answers by asking a chat model, with a fixed rubric."""

    def __init__(self, model: ChatModel, *, samples: int = 1) -> None:
        """Initialise the judge.

        Args:
            model: The model to ask. Should not be the model that produced the
                answers — models score their own family more generously — which
                the composition root refuses unless it is acknowledged.
            samples: How many times to ask each question. Above one, the majority
                verdict is taken and disagreement is reported.

        Raises:
            ValueError: If asked for fewer than one sample.
        """
        if samples < 1:
            msg = f"a judge must be asked at least once, not {samples} times"
            raise ValueError(msg)
        self._model = model
        self._samples = samples

    @property
    def model_id(self) -> str:
        """Which model judges."""
        return self._model.model_id

    async def judge_faithfulness(
        self, question: str, answer: str, references: Sequence[str]
    ) -> Judgement:
        """Decide whether the answer stays within what the references support."""
        prompt = (
            f"Question:\n{question}\n\n"
            f"Source passages:\n{render_references(references)}\n\n"
            f"Answer to grade:\n{answer}"
        )
        return await self._ask(FAITHFULNESS_RUBRIC, prompt)

    async def judge_relevance(self, question: str, answer: str) -> Judgement:
        """Decide whether the answer addresses the question."""
        prompt = f"Question:\n{question}\n\nAnswer to grade:\n{answer}"
        return await self._ask(RELEVANCE_RUBRIC, prompt)

    async def _ask(self, rubric: str, prompt: str) -> Judgement:
        """Put one question to the judge, as many times as configured."""
        verdicts: list[Verdict] = []
        reasons: list[str] = []
        for _ in range(self._samples):
            verdict, reasoning = await self._one(rubric, prompt)
            verdicts.append(verdict)
            reasons.append(reasoning)

        verdict, unanimous = majority(verdicts)
        return Judgement(
            verdict=verdict,
            # The first reasoning, not all of them joined: a paragraph made of
            # three arguments for possibly different verdicts is harder to read
            # than one argument, and the disagreement is already reported.
            reasoning=reasons[0],
            model_id=self.model_id,
            unanimous=unanimous,
            samples=self._samples,
        )

    async def _one(self, rubric: str, prompt: str) -> tuple[Verdict, str]:
        """Ask once, and read the reply."""
        try:
            completion = await self._model.complete(
                [
                    Message(role="system", content=rubric),
                    Message(role="user", content=prompt),
                ],
                temperature=TEMPERATURE,
            )
        except GenerationError as error:
            # The judge being unreachable is not evidence about the answer.
            return Verdict.UNDECIDED, f"the judge could not be reached: {error}"
        return parse_verdict(completion.text)


def parse_verdict(text: str) -> tuple[Verdict, str]:
    """Read a verdict out of a judge's reply.

    Returns:
        The verdict and its reasoning. Anything unreadable is ``UNDECIDED`` with
        the raw reply as the reasoning, so a rubric that a model keeps failing to
        follow is visible in the report rather than averaged away.
    """
    match = JSON_OBJECT.search(text)
    if match is None:
        return Verdict.UNDECIDED, f"unparseable reply: {text[:200]}"
    try:
        payload = json.loads(match.group())
    except json.JSONDecodeError:
        return Verdict.UNDECIDED, f"unparseable reply: {text[:200]}"
    if not isinstance(payload, dict):
        return Verdict.UNDECIDED, f"unparseable reply: {text[:200]}"

    reasoning = str(payload.get("reasoning", "")).strip()
    raw = str(payload.get("verdict", "")).strip().lower()
    try:
        verdict = Verdict(raw)
    except ValueError:
        return Verdict.UNDECIDED, reasoning or f"unknown verdict {raw!r}"
    if verdict is Verdict.UNDECIDED:
        # The rubric does not offer this label, so a model returning it has not
        # followed the rubric. Treated as an abstention either way.
        return Verdict.UNDECIDED, reasoning
    return verdict, reasoning


__all__ = ["JSON_OBJECT", "TEMPERATURE", "ModelAnswerJudge", "parse_verdict"]
