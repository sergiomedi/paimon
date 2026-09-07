"""A scriptable AnswerJudge."""

from collections.abc import Sequence

from paimon.evaluation.judging import Judgement, Verdict


class ScriptedJudge:
    """Returns prepared verdicts and records what it was asked.

    Scripted per question rather than per call, so a test can give one answer a
    different verdict from another without depending on call order.
    """

    def __init__(
        self,
        faithfulness: dict[str, Verdict] | None = None,
        relevance: dict[str, Verdict] | None = None,
        *,
        model_id: str = "judge-v1",
        default: Verdict = Verdict.YES,
    ) -> None:
        """Script the judge's answers."""
        self._faithfulness = faithfulness or {}
        self._relevance = relevance or {}
        self._model_id = model_id
        self._default = default
        self.references_seen: list[Sequence[str]] = []
        self.relevance_questions: list[str] = []

    @property
    def model_id(self) -> str:
        """Which model judges."""
        return self._model_id

    async def judge_faithfulness(
        self, question: str, answer: str, references: Sequence[str]
    ) -> Judgement:
        """Return the scripted faithfulness verdict."""
        self.references_seen.append(list(references))
        return Judgement(
            verdict=self._faithfulness.get(question, self._default),
            reasoning="scripted",
            model_id=self._model_id,
        )

    async def judge_relevance(self, question: str, answer: str) -> Judgement:
        """Return the scripted relevance verdict."""
        self.relevance_questions.append(question)
        return Judgement(
            verdict=self._relevance.get(question, self._default),
            reasoning="scripted",
            model_id=self._model_id,
        )
