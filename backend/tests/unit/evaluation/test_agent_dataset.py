"""The agent golden set, and the rules that keep it honest.

Half of this is about the loader. The other half runs against the **shipped**
file and the shipped corpus, because a dataset is the thing every measurement is
relative to: a quote that has drifted out of the corpus turns a correct system
into a failing one, silently, and the aggregate still looks like a measurement.
"""

import hashlib
import json
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


class TestAMultiHopTaskThatIsNot:
    """A hop goes between documents, and the label has to mean that.

    This rule exists because the mistake was made twice. agents-v1 shipped with
    seven of nine multi-hop tasks whose evidence sat in one document, and a test
    caught it. The held-out set, written a day later by the same author who had
    just fixed that, did it again with four of ten. Twice is a rule.

    What it costs to get wrong: the headline comparison of Phase 9 is
    multi-hop, and a category quietly full of one-hop tasks reports a multi-hop
    score for something else.
    """

    def test_one_document_is_not_a_hop(self) -> None:
        with pytest.raises(ValueError, match="every supporting passage comes from runbook"):
            task(
                category="multi-hop",
                supporting=(
                    SupportingPassage(document_id="runbook", quote="Cordon the node"),
                    SupportingPassage(document_id="runbook", quote="Drain it first"),
                ),
            )

    def test_two_documents_is(self) -> None:
        task(
            category="multi-hop",
            supporting=(
                SupportingPassage(document_id="runbook", quote="Cordon the node"),
                SupportingPassage(document_id="postmortem", quote="Drain it first"),
            ),
        )

    def test_the_message_says_how_to_fix_it(self) -> None:
        with pytest.raises(ValueError, match="relabel it or give it the passage"):
            task(category="multi-hop")

    def test_two_passages_from_one_document_is_still_one_hop(self) -> None:
        # Ordinary and common: a question whose answer is two sentences of one
        # runbook. The rule is about documents, not about passages.
        task(
            category="one-hop",
            supporting=(
                SupportingPassage(document_id="runbook", quote="Cordon the node"),
                SupportingPassage(document_id="runbook", quote="Drain it first"),
            ),
        )

    def test_two_documents_is_not_one_hop(self) -> None:
        # The other direction, and the one that hides work rather than
        # inventing it: a genuinely two-document task filed as one-hop is a
        # multi-hop task that will never be counted as one.
        with pytest.raises(ValueError, match="is 'one-hop' but its supporting passages come"):
            task(
                category="one-hop",
                supporting=(
                    SupportingPassage(document_id="runbook", quote="Cordon the node"),
                    SupportingPassage(document_id="postmortem", quote="Drain it first"),
                ),
            )

    def test_the_one_hop_message_says_how_to_fix_it(self) -> None:
        with pytest.raises(ValueError, match="relabel it 'multi-hop' or"):
            task(
                category="one-hop",
                supporting=(
                    SupportingPassage(document_id="runbook", quote="Cordon the node"),
                    SupportingPassage(document_id="postmortem", quote="Drain it first"),
                ),
            )

    def test_a_planted_two_document_one_hop_is_refused_by_the_loader(self, tmp_path: Path) -> None:
        path = tmp_path / "planted-one-hop.jsonl"
        path.write_text(
            json.dumps(
                {
                    "id": "bad",
                    "question": "what happened?",
                    "expected": "answer",
                    "category": "one-hop",
                    "supporting": [
                        {"document_id": "runbook", "quote": "Cordon the node"},
                        {"document_id": "postmortem", "quote": "Drain it first"},
                    ],
                    "reference": "the halves are in different documents",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="is 'one-hop' but its supporting passages"):
            AgentDataset.from_jsonl(path)

    def test_categories_that_claim_nothing_are_left_alone(self) -> None:
        # "exact-identifier" and "injection" describe what is being tested, not
        # where the evidence is, so there is nothing to check them against.
        task(
            category="exact-identifier",
            supporting=(
                SupportingPassage(document_id="runbook", quote="Cordon the node"),
                SupportingPassage(document_id="postmortem", quote="Drain it first"),
            ),
        )

    def test_a_planted_bad_task_is_refused_by_the_loader(self, tmp_path: Path) -> None:
        # Through from_jsonl, because that is the path a dataset actually
        # arrives by and a rule enforced only in the constructor is a rule a
        # file can walk past.
        path = tmp_path / "planted.jsonl"
        path.write_text(
            json.dumps(
                {
                    "id": "bad",
                    "question": "what happened?",
                    "expected": "answer",
                    "category": "multi-hop",
                    "supporting": [
                        {"document_id": "runbook", "quote": "Cordon the node"},
                        {"document_id": "runbook", "quote": "Drain it first"},
                    ],
                    "reference": "both halves are in the runbook",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="is 'multi-hop' but every supporting passage"):
            AgentDataset.from_jsonl(path)

    def test_the_shipped_sets_obey_it(self) -> None:
        # Both load, which is the assertion: loading is what enforces the rule.
        assert len(SHIPPED) >= MINIMUM_TASKS
        heldout = AgentDataset.from_jsonl(
            ROOT / "evaluation" / "datasets" / "agents-v2-heldout.jsonl"
        )
        assert {item.category for item in heldout} <= set(SHIPPED.categories)


HELDOUT = ROOT / "evaluation" / "datasets" / "agents-v2-heldout.jsonl"

#: What each shipped set's task content hashes to. Pinned here so a change to a
#: question, a quote, an expected outcome or a category has to be made twice —
#: once in the file and once here, in the same commit, where a reviewer sees it.
#:
#: Every measurement is relative to these tasks. A benchmark compared against a
#: report written before a task changed is comparing two different experiments
#: and reporting the difference as a result, and nothing in the numbers would
#: show it.
TASK_DIGESTS = {
    "agents-v1.jsonl": (30, "739984e347d2affbd5d2f21a975f0f51d029b417ffcf8ef90acb4f1ec0975a54"),
    "agents-v2-heldout.jsonl": (
        10,
        "792fe1d6f431d57931f74893598e97bc886c0baba61464204d6b4bf92bd81b56",
    ),
}


def task_digest(path: Path) -> tuple[int, str]:
    """Hash a dataset's task content, ignoring everything that is not a task.

    Comment lines are skipped and each remaining line is re-serialised
    canonically — keys sorted, no incidental whitespace — before hashing. So
    reformatting the file, or rewriting the header that explains what the set
    is for, leaves the digest alone, while changing a single character of a
    question, a quote or an expected outcome does not.

    That is the distinction worth enforcing: "held out" means nobody tuned a
    system against these tasks, not that the file may never be touched.
    """
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        canonical = json.dumps(
            json.loads(stripped), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        rows.append(canonical)
    return len(rows), hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


class TestTheShippedTasksAreWhatWasMeasured:
    """The datasets every reported number is relative to, pinned by content.

    A report is only comparable with another report over the same tasks. If a
    question is reworded between two runs the comparison is between two
    different experiments, the difference gets reported as a result, and
    nothing in the numbers reveals it. Changing a task on purpose is allowed —
    it just has to be visible, which means updating the digest in the same
    commit.
    """

    @pytest.mark.parametrize(("name", "expected"), sorted(TASK_DIGESTS.items()))
    def test_the_task_content_has_not_drifted(self, name: str, expected: tuple[int, str]) -> None:
        count, digest = task_digest(ROOT / "evaluation" / "datasets" / name)

        assert (count, digest) == expected, (
            f"{name} changed. If that was deliberate, update TASK_DIGESTS to "
            f"({count}, '{digest}') in this commit; every report measured "
            f"against the old tasks is no longer comparable with one measured "
            f"against the new ones."
        )

    def test_the_digest_ignores_comments_and_formatting(self, tmp_path: Path) -> None:
        # Otherwise the pin would fight every clarification to a header, and
        # the cheapest way to make CI green would be to stop explaining the set.
        plain = tmp_path / "plain.jsonl"
        plain.write_text('{"id": "h001", "expected": "answer"}\n', encoding="utf-8")
        commented = tmp_path / "commented.jsonl"
        commented.write_text(
            '// what this set is for\n//\n{"expected":"answer",   "id":"h001"}\n\n',
            encoding="utf-8",
        )

        assert task_digest(plain) == task_digest(commented)

    def test_the_digest_notices_a_reworded_question(self, tmp_path: Path) -> None:
        before = tmp_path / "before.jsonl"
        before.write_text('{"id": "h001", "question": "which runbook?"}\n', encoding="utf-8")
        after = tmp_path / "after.jsonl"
        after.write_text('{"id": "h001", "question": "which runbook applies?"}\n', encoding="utf-8")

        assert task_digest(before) != task_digest(after)

    def test_the_digest_notices_a_changed_expected_outcome(self, tmp_path: Path) -> None:
        # The one a reader is least likely to spot in a diff, and the one that
        # silently turns a correct refusal into a failure.
        before = tmp_path / "before.jsonl"
        before.write_text('{"id": "h010", "expected": "refuse"}\n', encoding="utf-8")
        after = tmp_path / "after.jsonl"
        after.write_text('{"id": "h010", "expected": "answer"}\n', encoding="utf-8")

        assert task_digest(before) != task_digest(after)

    def test_the_digest_notices_a_dropped_task(self, tmp_path: Path) -> None:
        both = tmp_path / "both.jsonl"
        both.write_text('{"id": "h001"}\n{"id": "h002"}\n', encoding="utf-8")
        one = tmp_path / "one.jsonl"
        one.write_text('{"id": "h001"}\n', encoding="utf-8")

        assert task_digest(both) != task_digest(one)

    def test_both_shipped_sets_are_pinned(self) -> None:
        # A new dataset that nobody pinned is a dataset that can drift, so the
        # guard covers the directory rather than a list someone remembers to
        # extend.
        shipped = {path.name for path in (ROOT / "evaluation" / "datasets").glob("agents-*.jsonl")}

        assert shipped == set(TASK_DIGESTS)
