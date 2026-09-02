"""Tests for the golden set."""

from pathlib import Path

import pytest

from paimon.evaluation import EvaluationCase, EvaluationDataset, SupportingPassage

LINE = (
    '{"id": "q001", "question": "What drains a node?", '
    '"supporting": [{"document_id": "runbook", "quote": "Cordon the node first."}]}'
)


class TestMatching:
    def test_a_quote_matches_its_document(self) -> None:
        passage = SupportingPassage(document_id="runbook", quote="Cordon the node first.")
        assert passage.is_supported_by("runbook", "Step 1. Cordon the node first. Step 2.")

    def test_a_quote_from_another_document_does_not_match(self) -> None:
        passage = SupportingPassage(document_id="runbook", quote="Cordon the node first.")
        assert not passage.is_supported_by("handbook", "Cordon the node first.")

    def test_matching_ignores_how_the_text_is_wrapped(self) -> None:
        """Chunk boundaries and reflowing change line breaks without changing the
        words. Ground truth that breaks when a paragraph is rewrapped is ground
        truth nobody maintains."""
        passage = SupportingPassage(
            document_id="runbook", quote="Cordon the node\nbefore rebooting it."
        )
        assert passage.is_supported_by("runbook", "Cordon the node before   rebooting it.")

    def test_matching_ignores_case(self) -> None:
        passage = SupportingPassage(document_id="runbook", quote="cordon the node")
        assert passage.is_supported_by("runbook", "Cordon The Node first.")


class TestLoading:
    def test_a_dataset_loads_from_jsonl(self, tmp_path: Path) -> None:
        path = tmp_path / "cases.jsonl"
        path.write_text(f"{LINE}\n", encoding="utf-8")

        dataset = EvaluationDataset.from_jsonl(path)

        assert len(dataset) == 1
        assert next(iter(dataset)).case_id == "q001"

    def test_blank_and_commented_lines_are_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "cases.jsonl"
        path.write_text(f"\n// a note about the set\n{LINE}\n\n", encoding="utf-8")

        assert len(EvaluationDataset.from_jsonl(path)) == 1

    def test_a_malformed_line_fails_loudly_and_says_where(self, tmp_path: Path) -> None:
        """A benchmark that silently skips broken cases reports an improvement
        that is really an absence."""
        path = tmp_path / "cases.jsonl"
        path.write_text(f"{LINE}\n{{not json\n", encoding="utf-8")

        with pytest.raises(ValueError, match="line 2 is not valid JSON"):
            EvaluationDataset.from_jsonl(path)

    def test_a_missing_field_fails_loudly(self, tmp_path: Path) -> None:
        path = tmp_path / "cases.jsonl"
        path.write_text('{"id": "q001"}\n', encoding="utf-8")

        with pytest.raises(ValueError, match="line 1"):
            EvaluationDataset.from_jsonl(path)


class TestValidation:
    def test_a_case_with_no_supporting_passage_is_refused(self) -> None:
        """It cannot be right or wrong, so it scores as a failure for every
        configuration and drags every average down for no reason."""
        with pytest.raises(ValueError, match="cannot be scored"):
            EvaluationCase(case_id="q1", question="why?", supporting=())

    def test_a_case_with_no_question_is_refused(self) -> None:
        with pytest.raises(ValueError, match="has no question"):
            EvaluationCase(
                case_id="q1",
                question="  ",
                supporting=(SupportingPassage(document_id="d", quote="q"),),
            )
