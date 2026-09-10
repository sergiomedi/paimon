"""The judge: its rubric, its parsing, and what it refuses to guess.

Everything here is a mitigation for something measured. The three-level scale,
the reasoning-before-verdict ordering, the abstention on an unreadable reply and
the refusal to break a tie all exist because a judge of this kind agreed with
human assessors 56% of the time and erred towards generosity (ADR-0031).
"""

import pytest

from paimon.domain.errors import GenerationError
from paimon.evaluation.judging import (
    COMPLETENESS_RUBRIC,
    FAITHFULNESS_RUBRIC,
    RELEVANCE_RUBRIC,
    Verdict,
    majority,
    render_references,
)
from paimon.evaluation.model_judge import ModelAnswerJudge, parse_verdict
from tests.fakes import FakeChatModel

QUESTION = "how do I drain a node?"
ANSWER = "Cordon the node first [1]."
REFERENCES = ["Cordon the node first so the scheduler stops placing new pods on it."]
#: Wider than the golden quote, which is the point: a source list is what the
#: model was shown, and it carries material the golden set never names.
SOURCES = [
    "Cordon the node first so the scheduler stops placing new pods on it. "
    "Once the node is cordoned, evict the running pods."
]


def judge(reply: str, *, samples: int = 1, model_id: str = "judge-v1") -> ModelAnswerJudge:
    return ModelAnswerJudge(FakeChatModel(answer=reply, model_id=model_id), samples=samples)


class TestTheScale:
    def test_undecided_has_no_score(self) -> None:
        # Rather than a default that would quietly become a data point.
        with pytest.raises(ValueError, match="undecided"):
            _ = Verdict.UNDECIDED.score

    def test_partial_sits_between_the_two(self) -> None:
        # The middle label exists because forcing a binary choice on a partly
        # supported answer pushes a judge towards the generous end.
        assert Verdict.NO.score < Verdict.PARTIAL.score < Verdict.YES.score


class TestTheRubric:
    def test_it_asks_for_reasoning_before_the_verdict(self) -> None:
        # So the label is written after the argument rather than justified
        # afterwards. The order in the JSON is the mechanism.
        for rubric in (FAITHFULNESS_RUBRIC, COMPLETENESS_RUBRIC, RELEVANCE_RUBRIC):
            assert rubric.index('"reasoning"') < rubric.index('"verdict"')
            assert "Write the reasoning first" in rubric

    def test_it_forbids_answering_from_world_knowledge(self) -> None:
        assert "Do not use anything you know about the subject" in FAITHFULNESS_RUBRIC

    def test_it_says_length_is_not_quality(self) -> None:
        # Verbosity bias, named in the rubric rather than hoped away.
        assert "Length is not quality" in FAITHFULNESS_RUBRIC
        assert "Length is not quality" in RELEVANCE_RUBRIC

    def test_refusing_to_answer_is_not_an_unsupported_claim(self) -> None:
        # Otherwise the platform's most honest behaviour scores worst.
        assert "refusing is not an unsupported claim" in FAITHFULNESS_RUBRIC

    def test_references_are_numbered_for_the_judge(self) -> None:
        assert render_references(["first", "second"]).startswith("[1] first")

    def test_no_references_says_so_rather_than_rendering_nothing(self) -> None:
        assert render_references([]) == "(no passages were provided)"


class TestReadingAReply:
    def test_a_well_formed_reply_is_read(self) -> None:
        verdict, reasoning = parse_verdict('{"reasoning": "it matches", "verdict": "yes"}')
        assert verdict is Verdict.YES
        assert reasoning == "it matches"

    def test_json_wrapped_in_prose_is_still_read(self) -> None:
        # Models do this often enough that refusing would throw away usable
        # verdicts.
        verdict, _ = parse_verdict(
            'Sure! ```json\n{"reasoning": "ok", "verdict": "partial"}\n``` Hope that helps.'
        )
        assert verdict is Verdict.PARTIAL

    def test_an_unreadable_reply_abstains_rather_than_defaulting(self) -> None:
        # The important one. A judge whose malformed output silently became
        # "yes" would be worse than no judge: the failure looks like a pass.
        verdict, reasoning = parse_verdict("I think it is probably fine")
        assert verdict is Verdict.UNDECIDED
        assert "unparseable" in reasoning

    def test_an_unknown_label_abstains(self) -> None:
        verdict, _ = parse_verdict('{"reasoning": "hmm", "verdict": "sort of"}')
        assert verdict is Verdict.UNDECIDED

    def test_malformed_json_abstains(self) -> None:
        assert parse_verdict('{"verdict": yes')[0] is Verdict.UNDECIDED


class TestTheTwoHalves:
    """Faithfulness and completeness are precision and recall, and they differ.

    Not two spellings of one question: they are graded against different
    evidence and they disagree about the same answer on purpose. The clearest
    case is a refusal, which invents nothing and conveys nothing.
    """

    def test_a_refusal_is_faithful_and_incomplete(self) -> None:
        assert 'is "yes" — refusing is not an unsupported claim' in FAITHFULNESS_RUBRIC
        assert 'declines to answer is "no"' in COMPLETENESS_RUBRIC

    def test_faithfulness_asks_about_the_sources_and_completeness_about_the_passages(
        self,
    ) -> None:
        # The wording a judge is given decides what it grades. These two are the
        # sentences that keep the rubrics pointed at different evidence.
        assert "numbered sources" in FAITHFULNESS_RUBRIC
        assert "reference passages" in COMPLETENESS_RUBRIC
        assert "numbered sources" not in COMPLETENESS_RUBRIC

    def test_extra_material_is_a_question_only_for_faithfulness(self) -> None:
        # An elaboration beyond the golden passage is not an omission, and a
        # completeness rubric that penalised it would rebuild the defect
        # ADR-0033 removed.
        assert "Extra material is not your concern here" in COMPLETENESS_RUBRIC
        assert "detail is not invention" in FAITHFULNESS_RUBRIC


class TestAsking:
    async def test_a_verdict_carries_the_model_that_produced_it(self) -> None:
        # On every judgement, not only in the header: a header is the first thing
        # lost when numbers are copied into a table.
        judged = await judge(
            '{"reasoning": "ok", "verdict": "yes"}', model_id="qwen-judge"
        ).judge_faithfulness(QUESTION, ANSWER, REFERENCES)
        assert judged.model_id == "qwen-judge"

    async def test_an_unreachable_judge_is_not_evidence_about_the_answer(self) -> None:
        class Broken:
            model_id = "broken-v1"

            async def complete(self, messages: object, **_: object) -> object:
                msg = "the provider did not answer"
                raise GenerationError(msg)

        judged = await ModelAnswerJudge(Broken()).judge_faithfulness(  # type: ignore[arg-type]
            QUESTION, ANSWER, REFERENCES
        )
        assert judged.verdict is Verdict.UNDECIDED
        assert "could not be reached" in judged.reasoning

    async def test_faithfulness_is_shown_the_sources_it_grades_against(self) -> None:
        model = FakeChatModel(answer='{"reasoning": "ok", "verdict": "yes"}')
        await ModelAnswerJudge(model).judge_faithfulness(QUESTION, ANSWER, SOURCES)
        sent = "\n".join(message.content for message in model.calls[0])
        assert SOURCES[0] in sent
        assert FAITHFULNESS_RUBRIC in sent

    async def test_completeness_is_shown_the_golden_passages(self) -> None:
        model = FakeChatModel(answer='{"reasoning": "ok", "verdict": "yes"}')
        await ModelAnswerJudge(model).judge_completeness(QUESTION, ANSWER, REFERENCES)
        sent = "\n".join(message.content for message in model.calls[0])
        assert REFERENCES[0] in sent
        assert COMPLETENESS_RUBRIC in sent

    async def test_the_sources_keep_the_numbering_the_answer_s_markers_use(self) -> None:
        # So [2] in the answer names [2] in the prompt and the judge can follow
        # one to the other instead of guessing.
        model = FakeChatModel(answer='{"reasoning": "ok", "verdict": "yes"}')
        await ModelAnswerJudge(model).judge_faithfulness(
            QUESTION, "Evict them [2].", ["first source", "second source"]
        )
        sent = "\n".join(message.content for message in model.calls[0])
        assert "[1] first source" in sent
        assert "[2] second source" in sent

    async def test_relevance_is_judged_without_the_expected_passages(self) -> None:
        # Handing them over would let the judge reward an answer for matching
        # them rather than for answering the question.
        model = FakeChatModel(answer='{"reasoning": "ok", "verdict": "yes"}')
        await ModelAnswerJudge(model).judge_relevance(QUESTION, ANSWER)
        sent = "\n".join(message.content for message in model.calls[0])
        assert REFERENCES[0] not in sent

    async def test_asking_zero_times_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least once"):
            ModelAnswerJudge(FakeChatModel(), samples=0)

    async def test_repeated_samples_are_reported(self) -> None:
        judged = await judge('{"reasoning": "ok", "verdict": "yes"}', samples=3).judge_relevance(
            QUESTION, ANSWER
        )
        assert judged.samples == 3
        assert judged.unanimous


class TestCombiningSamples:
    def test_a_clear_majority_wins(self) -> None:
        verdict, unanimous = majority([Verdict.YES, Verdict.YES, Verdict.NO])
        assert verdict is Verdict.YES
        assert not unanimous

    def test_unanimity_is_reported(self) -> None:
        assert majority([Verdict.NO, Verdict.NO])[1]

    def test_a_tie_is_undecided_rather_than_broken(self) -> None:
        # Breaking it towards the generous label biases the aggregate in exactly
        # the direction judges already err; breaking it the other way is a
        # different arbitrary rule. A judge that cannot decide has said something.
        assert majority([Verdict.YES, Verdict.NO])[0] is Verdict.UNDECIDED

    def test_abstentions_do_not_outvote_real_verdicts(self) -> None:
        verdict, _ = majority([Verdict.UNDECIDED, Verdict.UNDECIDED, Verdict.PARTIAL])
        assert verdict is Verdict.PARTIAL

    def test_nothing_usable_is_undecided(self) -> None:
        assert majority([Verdict.UNDECIDED])[0] is Verdict.UNDECIDED
