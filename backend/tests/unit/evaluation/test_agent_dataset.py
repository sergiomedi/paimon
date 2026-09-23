"""The agent golden set, and the rules that keep it honest.

Half of this is about the loader. The other half runs against the **shipped**
file and the shipped corpus, because a dataset is the thing every measurement is
relative to: a quote that has drifted out of the corpus turns a correct system
into a failing one, silently, and the aggregate still looks like a measurement.
"""

from pathlib import Path

import pytest

from paimon.evaluation.agent_dataset import AgentDataset, AgentTask, Outcome
from paimon.evaluation.dataset import SupportingPassage, normalize

ROOT = Path(__file__).resolve().parents[4]
CORPUS = ROOT / "evaluation" / "corpus" / "sample"
DATASET = ROOT / "evaluation" / "datasets" / "agents-v1.jsonl"

SHIPPED = AgentDataset.from_jsonl(DATASET)
DOCUMENTS = {
    path.stem: normalize(path.read_text(encoding="utf-8")) for path in sorted(CORPUS.glob("*.md"))
}

#: What the set is required to cover. Named here rather than derived from the
#: file, so deleting every injection task fails a test instead of quietly
#: shrinking what the benchmark measures.
REQUIRED_CATEGORIES = frozenset(
    {"one-hop", "multi-hop", "out-of-corpus", "exact-identifier", "injection"}
)

#: Anthropic's guidance on agent evals asks for twenty to thirty tasks before
#: pass^k over them means much. Below the floor the intervals swallow every
#: result; far above it, a local model cannot finish five trials in an evening.
MINIMUM_TASKS = 20
MAXIMUM_TASKS = 40


def task(**overrides: object) -> AgentTask:
    values: dict[str, object] = {
        "task_id": "t1",
        "question": "What do I do first?",
        "expected": Outcome.ANSWER,
        "category": "one-hop",
        "supporting": (SupportingPassage(document_id="runbook", quote="Cordon the node"),),
        "reference": "Cordon it.",
    }
    values.update(overrides)
    return AgentTask(**values)  # type: ignore[arg-type]


class TestATaskThatCannotBeScored:
    def test_an_answerable_task_needs_a_supporting_passage(self) -> None:
        with pytest.raises(ValueError, match="names no supporting passage"):
            task(supporting=())

    def test_a_refusal_task_must_not_name_one(self) -> None:
        # If the corpus supports an answer, the expectation is wrong — and a
        # task that expects a refusal it could answer marks a correct system
        # down for being correct.
        with pytest.raises(ValueError, match="expects a refusal and names supporting"):
            task(expected=Outcome.REFUSE)

    def test_every_task_needs_a_reference_solution(self) -> None:
        # The rule that keeps the set honest: a task the author never solved
        # grades every system identically and measures none of them.
        with pytest.raises(ValueError, match="has no reference solution"):
            task(reference="  ")

    def test_a_task_needs_a_category(self) -> None:
        with pytest.raises(ValueError, match="requires a non-empty category"):
            task(category="")


class TestLoading:
    def test_a_repeated_id_is_refused(self, tmp_path: Path) -> None:
        # Two tasks under one id make a paired comparison line up the wrong
        # rows, which is a wrong number with a confident interval around it.
        line = (
            '{"id": "x", "question": "q", "expected": "refuse", "category": "c", "reference": "r"}'
        )
        path = tmp_path / "dupes.jsonl"
        path.write_text(f"{line}\n{line}\n", encoding="utf-8")

        with pytest.raises(ValueError, match="more than once: x"):
            AgentDataset.from_jsonl(path)

    def test_comments_and_blank_lines_are_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "commented.jsonl"
        path.write_text(
            '// a note about the set\n\n{"id": "x", "question": "q", '
            '"expected": "refuse", "category": "c", "reference": "r"}\n',
            encoding="utf-8",
        )

        assert len(AgentDataset.from_jsonl(path)) == 1

    def test_a_bad_expectation_names_what_is_allowed(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        path.write_text(
            '{"id": "x", "question": "q", "expected": "maybe", "category": "c", '
            '"reference": "r"}\n',
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="answer, refuse"):
            AgentDataset.from_jsonl(path)

    def test_malformed_json_names_the_line(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.jsonl"
        path.write_text("{not json}\n", encoding="utf-8")

        with pytest.raises(ValueError, match="line 1 is not valid JSON"):
            AgentDataset.from_jsonl(path)


class TestTheShippedSet:
    """Run against the file that will actually be used."""

    def test_it_loads(self) -> None:
        assert len(SHIPPED) >= MINIMUM_TASKS
        assert len(SHIPPED) <= MAXIMUM_TASKS

    def test_every_required_category_is_present(self) -> None:
        assert set(SHIPPED.categories) >= REQUIRED_CATEGORIES

    def test_every_category_has_more_than_one_task(self) -> None:
        # A category of one is a category whose rate is 0% or 100% and whose
        # interval is meaningless.
        assert all(len(tasks) > 1 for tasks in SHIPPED.by_category().values())

    def test_some_tasks_must_be_refused(self) -> None:
        # The cases where the agent must *not* act. A set without them measures
        # only whether a system answers, and every system answers.
        refusals = [item for item in SHIPPED if item.expected is Outcome.REFUSE]
        assert len(refusals) >= 3

    @pytest.mark.parametrize("item", SHIPPED.tasks, ids=lambda item: item.task_id)
    def test_every_anchored_quote_is_really_in_the_corpus(self, item: AgentTask) -> None:
        # The check that stops the set rotting. A quote edited out of a document
        # turns a correct system into a failing one, silently, and the aggregate
        # still looks like a measurement. Parametrised so the failure names the
        # task rather than reporting that something somewhere is wrong.
        for passage in item.supporting:
            document = DOCUMENTS.get(passage.document_id)
            assert document is not None, f"unknown document '{passage.document_id}'"
            assert normalize(passage.quote) in document, (
                f"'{passage.quote[:60]}...' is no longer in {passage.document_id}"
            )

    @pytest.mark.parametrize("item", SHIPPED.tasks, ids=lambda item: item.task_id)
    def test_every_task_carries_a_reference_solution(self, item: AgentTask) -> None:
        # Enforced by the type too; asserted here so the failure names the task
        # and so the rule is visible to someone reading the tests rather than
        # the dataclass.
        assert item.reference.strip()

    def test_the_multi_hop_tasks_really_span_documents(self) -> None:
        # A "multi-hop" task whose evidence sits in one document is a one-hop
        # task with an ambitious label, and it would make the headline
        # comparison of this phase meaningless.
        spanning = [
            item for item in SHIPPED if item.category == "multi-hop" and len(item.documents) > 1
        ]
        assert len(spanning) >= 3

    def test_the_injection_tasks_point_at_the_injected_document(self) -> None:
        injections = [item for item in SHIPPED if item.category == "injection"]
        assert injections
        assert any(
            "vendor-integration-notes" in item.question or "Northwind" in item.question
            for item in injections
        )
