"""Graders that can fail, and three systems built to make them.

A grader nobody has seen fail is a grader nobody should trust. If a suite reports
that everything scores well, "the systems are good" and "the graders approve of
anything" look identical from outside — so this file runs a system that must
score one, a system that must score one only where refusing is right, and a
system that must score zero everywhere, and asserts each shape.

The last of those is the one that matters most. A confident answer with nothing
behind it must not be graded as a cautious refusal, or the worst possible system
would be reported as the safest one.
"""

from pathlib import Path

from paimon.application.use_cases.answer_question import NO_MATERIAL
from paimon.domain.value_objects import Citation
from paimon.evaluation.agent_benchmark import run_agent_benchmark
from paimon.evaluation.agent_dataset import AgentDataset, AgentTask, Outcome
from paimon.evaluation.agent_grading import (
    REFUSALS,
    Attempt,
    AttemptOutcome,
    Trajectory,
    grade,
    support_coverage,
)
from paimon.evaluation.agent_systems import AlwaysAnswersUncited, AlwaysRefuses, Oracle
from paimon.evaluation.dataset import SupportingPassage

CORPUS = Path(__file__).resolve().parents[4] / "evaluation" / "corpus" / "sample"
DATASET = Path(__file__).resolve().parents[4] / "evaluation" / "datasets" / "agents-v1.jsonl"

RUNBOOK = "Cordon the node first so the scheduler stops placing new pods on it."
_HEADING = "# Node maintenance\n\n"
DOCUMENTS = {"runbook": f"{_HEADING}{RUNBOOK}\n"}
#: Where the runbook sentence actually starts. Computed, not counted by hand —
#: a citation's offsets are the one thing in a test that must not be guessed.
RUNBOOK_AT = len(_HEADING)

#: The shipped corpus and dataset, read once. Module level rather than fixtures
#: because a class-scoped fixture and a function-scoped event loop do not mix,
#: and these are immutable anyway.
DOCS = {path.stem: path.read_text(encoding="utf-8") for path in sorted(CORPUS.glob("*.md"))}
TASKS = AgentDataset.from_jsonl(DATASET)


def answerable(**overrides: object) -> AgentTask:
    values: dict[str, object] = {
        "task_id": "t1",
        "question": "What do I do first?",
        "expected": Outcome.ANSWER,
        "category": "one-hop",
        "supporting": (SupportingPassage(document_id="runbook", quote="Cordon the node first"),),
        "reference": "Cordon it first.",
    }
    values.update(overrides)
    return AgentTask(**values)  # type: ignore[arg-type]


def refusable() -> AgentTask:
    return AgentTask(
        task_id="t2",
        question="What is our ingress rate limit?",
        expected=Outcome.REFUSE,
        category="out-of-corpus",
        reference="Nothing in the corpus covers ingress.",
    )


def cited(
    quote: str = RUNBOOK, *, document_id: str = "runbook", start: int = RUNBOOK_AT
) -> Citation:
    return Citation(
        marker=1,
        document_id=document_id,
        chunk_id=f"{document_id}:0",
        source_uri=document_id,
        title=document_id,
        heading_path=(),
        start_char=start,
        end_char=start + len(quote),
        quote=quote,
    )


def attempt(text: str, *citations: Citation, failed: str = "") -> Attempt:
    return Attempt(
        task_id="t1",
        trial=1,
        text=text,
        citations=citations,
        trajectory=Trajectory(stop_reason="answered"),
        failed=failed,
    )


class TestWhatAnAttemptDid:
    def test_citing_something_is_answering(self) -> None:
        assert attempt("Cordon it [1].", cited()).outcome is AttemptOutcome.ANSWERED

    def test_the_platforms_own_refusal_is_a_refusal(self) -> None:
        assert attempt(NO_MATERIAL).outcome is AttemptOutcome.REFUSED

    def test_claims_with_no_citations_are_neither(self) -> None:
        # The distinction the grading rests on. Collapsing this into "refused"
        # would make the worst possible system score best on the tasks whose
        # right answer is "I cannot".
        assert attempt("You should cordon the node.").outcome is AttemptOutcome.UNGROUNDED

    def test_a_refusal_is_matched_exactly_not_by_keyword(self) -> None:
        # "the runbook says you cannot raise the ceiling" is a correct, cited
        # answer. A rule that looked for "cannot" would grade it as a refusal.
        text = "The runbook says you cannot raise the ceiling without review [1]."
        assert attempt(text, cited()).outcome is AttemptOutcome.ANSWERED

    def test_every_refusal_the_platform_says_is_recognised(self) -> None:
        # Collected from the modules that define them, so a reworded refusal
        # cannot silently start being graded as a confident answer.
        assert all(attempt(text).outcome is AttemptOutcome.REFUSED for text in REFUSALS)


class TestGradingOneAttempt:
    def test_a_cited_answer_that_covers_the_passage_passes(self) -> None:
        result = grade(answerable(), attempt("Cordon it first [1].", cited()), DOCUMENTS)

        assert result.passed
        assert result.support_coverage == 1.0
        assert result.citation_precision == 1.0

    def test_citing_the_wrong_passage_of_the_right_document_fails(self) -> None:
        # The case the review asked for. The citation resolves — it points at
        # real text in the right document — and it does not support the claim,
        # which is indistinguishable from a good citation to anyone who does not
        # open it.
        elsewhere = "# Node maintenance"
        result = grade(
            answerable(),
            attempt("Cordon it first [1].", cited(elsewhere, start=0)),
            DOCUMENTS,
        )

        assert result.citation_precision == 1.0
        assert result.support_coverage == 0.0
        assert not result.passed

    def test_covering_some_of_the_passages_is_not_covering_them(self) -> None:
        task = answerable(
            supporting=(
                SupportingPassage(document_id="runbook", quote="Cordon the node first"),
                SupportingPassage(document_id="runbook", quote="stops placing new pods"),
            )
        )
        partial = cited("Cordon the node first")

        result = grade(task, attempt("Cordon it [1].", partial), DOCUMENTS)

        assert result.support_coverage == 0.5
        assert not result.passed

    def test_a_citation_that_does_not_resolve_fails(self) -> None:
        invented = cited("Drain the node first")

        result = grade(answerable(), attempt("Drain it [1].", invented), DOCUMENTS)

        assert result.citation_precision == 0.0
        assert not result.passed

    def test_refusing_an_answerable_task_fails(self) -> None:
        assert not grade(answerable(), attempt(NO_MATERIAL), DOCUMENTS).passed

    def test_refusing_an_unanswerable_task_passes(self) -> None:
        assert grade(refusable(), attempt(NO_MATERIAL), DOCUMENTS).passed

    def test_answering_an_unanswerable_task_fails(self) -> None:
        assert not grade(refusable(), attempt("It is 1000/s [1].", cited()), DOCUMENTS).passed

    def test_an_ungrounded_answer_fails_a_refusal_task_too(self) -> None:
        assert not grade(refusable(), attempt("It is 1000 per second."), DOCUMENTS).passed

    def test_a_crashed_run_is_not_a_refusal(self) -> None:
        # Otherwise an unreliable system scores well on exactly the tasks whose
        # right answer is "I cannot".
        crashed = attempt("", failed="GenerationError: provider unreachable")

        assert not grade(refusable(), crashed, DOCUMENTS).passed

    def test_coverage_of_a_refusal_task_is_not_zero(self) -> None:
        # There is nothing to cover. Reporting zero would drag the average down
        # for doing the right thing.
        assert support_coverage(refusable(), ()) == 1.0


class TestTheGradersCanFail:
    """The three stand-ins, run over the real dataset and the real corpus.

    Not the small fixture above: these load the shipped file. A grader that
    works on a hand-made example and not on the dataset it will be used with is
    a grader that has not been checked.
    """

    async def test_the_oracle_scores_one_on_everything(self) -> None:
        # The check most easily skipped, because a suite where nothing fails
        # looks finished. When this drops below one, the grader is wrong or the
        # dataset is — an anchored quote that is not in the corpus, a coverage
        # rule nothing can satisfy — and the failure names the task.
        report = await run_agent_benchmark(TASKS, Oracle(DOCS, NO_MATERIAL), DOCS, trials=1)

        assert [task.task_id for task in report.never_passed] == []
        assert report.reliability.pass_at_1.mean == 1.0

    async def test_the_oracle_scores_one_in_every_category(self) -> None:
        report = await run_agent_benchmark(TASKS, Oracle(DOCS, NO_MATERIAL), DOCS, trials=1)

        assert {
            item.category: item.reliability.pass_at_1.mean for item in report.categories
        } == dict.fromkeys(TASKS.categories, 1.0)

    async def test_always_refusing_passes_only_where_refusing_is_right(self) -> None:
        report = await run_agent_benchmark(TASKS, AlwaysRefuses(NO_MATERIAL), DOCS, trials=1)

        by_category = {item.category: item.reliability.pass_at_1.mean for item in report.categories}
        assert by_category["out-of-corpus"] == 1.0
        assert by_category["one-hop"] == 0.0
        assert by_category["multi-hop"] == 0.0
        assert by_category["exact-identifier"] == 0.0

    async def test_always_refusing_does_not_score_well_overall(self) -> None:
        # Caution is not correctness. A grader that rewarded this would reward a
        # system for never answering anything.
        report = await run_agent_benchmark(TASKS, AlwaysRefuses(NO_MATERIAL), DOCS, trials=1)

        assert report.reliability.pass_at_1.mean < 0.25

    async def test_answering_without_citations_scores_zero_everywhere(self) -> None:
        # Including on the refusal tasks. Its output there is not a refusal, it
        # is an invention, and a grader that could not tell them apart would
        # report the worst possible system as the safest one.
        report = await run_agent_benchmark(TASKS, AlwaysAnswersUncited(), DOCS, trials=1)

        assert report.reliability.pass_at_1.mean == 0.0
        assert all(item.reliability.pass_at_1.mean == 0.0 for item in report.categories)

    async def test_the_uncited_answerer_is_reported_as_ungrounded(self) -> None:
        # Not merely failing — failing *for the right reason*, which is what a
        # person reading the report needs in order to act on it.
        report = await run_agent_benchmark(TASKS, AlwaysAnswersUncited(), DOCS, trials=1)

        assert report.trajectory.outcomes == {"ungrounded": len(TASKS)}

    async def test_the_refuser_and_the_inventor_are_told_apart(self) -> None:
        refuser = await run_agent_benchmark(TASKS, AlwaysRefuses(NO_MATERIAL), DOCS, trials=1)
        inventor = await run_agent_benchmark(TASKS, AlwaysAnswersUncited(), DOCS, trials=1)

        assert refuser.trajectory.outcomes == {"refused": len(TASKS)}
        assert inventor.trajectory.outcomes == {"ungrounded": len(TASKS)}
