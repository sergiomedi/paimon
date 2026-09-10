"""The command lines this tool refuses, and how it says a run is still running.

Every combination refused here was previously **accepted and ignored**, which is
the worse failure: `--answers` without `--corpus` printed a complete, confident
report saying 0.0% citation accuracy for a system whose citations were all
correct, because there was no corpus to open them against. A tool that produces
a plausible report of the wrong thing is harder to catch than one that stops.
"""

import argparse
import io
import json
from pathlib import Path

from tests.fakes import ScriptedJudge

from paimon.interfaces.cli.evaluate import (
    USAGE_ERROR,
    judging_for,
    progress_reporter,
    unusable,
)


def command(**overrides: object) -> argparse.Namespace:
    """A retrieval run with every optional flag off, plus the ones under test."""
    defaults: dict[str, object] = {
        "answers": False,
        "corpus": None,
        "labels": None,
        "write_labels": None,
        "against": None,
    }
    return argparse.Namespace(**{**defaults, **overrides})


class TestWhatIsRefused:
    def test_a_plain_retrieval_run_is_fine(self) -> None:
        assert unusable(command(), judge_enabled=False) is None

    def test_answers_without_a_corpus_is_refused_with_the_reason(self) -> None:
        # The one that actually happened. The message has to say why, because
        # "0.0% citation accuracy" is a believable number for a broken system
        # and somebody will go looking for the bug in the wrong place.
        refusal = unusable(command(answers=True), judge_enabled=False)
        assert refusal is not None
        assert "--corpus" in refusal
        assert "0.0% citation accuracy" in refusal

    def test_answers_with_a_corpus_is_fine(self) -> None:
        assert unusable(command(answers=True, corpus=Path("corpus")), judge_enabled=False) is None

    def test_labels_without_answers_is_refused(self) -> None:
        # Labelling is about answers, and a retrieval run has none.
        refusal = unusable(command(labels=Path("l.jsonl")), judge_enabled=True)
        assert refusal is not None
        assert "--answers" in refusal

    def test_writing_labels_without_answers_is_refused(self) -> None:
        refusal = unusable(command(write_labels=Path("l.jsonl")), judge_enabled=True)
        assert refusal is not None
        assert "--answers" in refusal

    def test_writing_and_reading_labels_at_once_is_refused(self) -> None:
        # One writes a blank template, the other reads a filled one.
        refusal = unusable(
            command(answers=True, corpus=Path("c"), labels=Path("l"), write_labels=Path("l")),
            judge_enabled=True,
        )
        assert refusal is not None
        assert "opposite things" in refusal

    def test_comparing_an_answering_run_is_refused_rather_than_ignored(self) -> None:
        # --against compares retrieval runs. On the answering path it did
        # nothing at all, which reads as "no difference found".
        refusal = unusable(
            command(answers=True, corpus=Path("c"), against=Path("baseline.json")),
            judge_enabled=False,
        )
        assert refusal is not None
        assert "--against" in refusal

    def test_labels_without_a_judge_names_the_setting_to_change(self) -> None:
        # Calibration compares a person's verdicts with the judge's. With the
        # judge off there are none, and the labels were silently dropped.
        refusal = unusable(
            command(answers=True, corpus=Path("c"), labels=Path("l.jsonl")),
            judge_enabled=False,
        )
        assert refusal is not None
        assert "PAIMON_EVALUATION__JUDGE__ENABLED" in refusal

    def test_labels_with_a_judge_is_fine(self) -> None:
        assert (
            unusable(
                command(answers=True, corpus=Path("c"), labels=Path("l.jsonl")),
                judge_enabled=True,
            )
            is None
        )

    def test_a_refusal_is_not_the_same_exit_code_as_an_empty_run(self) -> None:
        # 1 means the benchmark ran and scored nothing, which is a result.
        assert USAGE_ERROR != 1


class TestWhetherToJudge:
    def test_writing_a_template_does_not_ask_the_judge(self) -> None:
        # The template is blank on purpose, so every verdict bought here would
        # be discarded — three model calls per case for nothing.
        judging = judging_for(
            command(answers=True, write_labels=Path("l.jsonl")),
            ScriptedJudge(),
            self_judged=False,
        )
        assert judging.judge is None

    def test_a_normal_run_keeps_the_judge(self) -> None:
        judge = ScriptedJudge()
        judging = judging_for(command(answers=True), judge, self_judged=True)
        assert judging.judge is judge
        assert judging.self_judged

    def test_labels_are_loaded_when_given(self, tmp_path: Path) -> None:
        path = tmp_path / "labels.jsonl"
        path.write_text(
            json.dumps({"case_id": "q1", "faithfulness": "yes"}) + "\n", encoding="utf-8"
        )
        judging = judging_for(
            command(answers=True, labels=path), ScriptedJudge(), self_judged=False
        )
        assert [label.case_id for label in judging.labels] == ["q1"]


class Terminal(io.StringIO):
    """A stream that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


class TestProgress:
    def test_a_pipe_gets_one_line_per_case(self) -> None:
        # A carriage return in a CI log crushes the whole run onto one
        # unreadable line.
        stream = io.StringIO()
        report = progress_reporter("answering", stream)
        report(done=1, total=2, case_id="q1")
        report(done=2, total=2, case_id="q2")

        assert "\r" not in stream.getvalue()
        assert stream.getvalue().splitlines() == ["answering 1/2  q1", "answering 2/2  q2"]

    def test_a_terminal_gets_the_counter_in_place(self) -> None:
        stream = Terminal()
        report = progress_reporter("answering", stream)
        report(done=1, total=2, case_id="q1")
        report(done=2, total=2, case_id="q2")

        written = stream.getvalue()
        assert written.startswith("\r")
        assert "answering 2/2  q2" in written
        # Exactly one newline, at the end: the line is rewritten until the run
        # finishes, and then it is left standing.
        assert written.count("\n") == 1
        assert written.endswith("\n")

    def test_the_counter_erases_what_it_overwrites(self) -> None:
        # A short case id printed over a long one would otherwise leave the
        # tail of the long one behind, which reads as a corrupted id.
        stream = Terminal()
        progress_reporter("answering", stream)(done=1, total=9, case_id="q1")
        assert "\x1b[K" in stream.getvalue()
