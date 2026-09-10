"""A scriptable AnswerJudge."""

from collections.abc import Sequence

from paimon.evaluation.judging import Judgement, Verdict


class ScriptedJudge:
    """Returns prepared verdicts and records what it was asked.

    Scripted per question rather than per call, so a test can give one answer a
    different verdict from another without depending on call order.

    What it was *shown* is recorded separately per rubric, because that is the
    thing worth pinning: faithfulness must be asked about the sources the model
    saw and completeness about the golden passages, and a fake that lumped them
    into one list could not tell a passing run from the defect in ADR-0033.
    """

    def __init__(
        self,
        faithfulness: dict[str, Verdict] | None = None,
        relevance: dict[str, Verdict] | None = None,
        completeness: dict[str, Verdict] | None = None,
        *,
        model_id: str = "judge-v1",
        default: Verdict = Verdict.YES,
    ) -> None:
        """Script the judge's answers."""
        self._faithfulness = faithfulness or {}
        self._relevance = relevance or {}
        self._completeness = completeness or {}
        self._model_id = model_id
        self._default = default
        self.sources_seen: list[Sequence[str]] = []
        """What faithfulness was graded against, per call."""
        self.references_seen: list[Sequence[str]] = []
        """What completeness was graded against, per call."""
        self.relevance_questions: list[str] = []

    @property
    def model_id(self) -> str:
        """Which model judges."""
        return self._model_id

    async def judge_faithfulness(
        self, question: str, answer: str, sources: Sequence[str]
    ) -> Judgement:
        """Return the scripted faithfulness verdict."""
        self.sources_seen.append(list(sources))
        return self._verdict(self._faithfulness, question)

    async def judge_completeness(
        self, question: str, answer: str, references: Sequence[str]
    ) -> Judgement:
        """Return the scripted completeness verdict."""
        self.references_seen.append(list(references))
        return self._verdict(self._completeness, question)

    async def judge_relevance(self, question: str, answer: str) -> Judgement:
        """Return the scripted relevance verdict."""
        self.relevance_questions.append(question)
        return self._verdict(self._relevance, question)

    def _verdict(self, scripted: dict[str, Verdict], question: str) -> Judgement:
        """The scripted verdict for this question, or the default."""
        return Judgement(
            verdict=scripted.get(question, self._default),
            reasoning="scripted",
            model_id=self._model_id,
        )
