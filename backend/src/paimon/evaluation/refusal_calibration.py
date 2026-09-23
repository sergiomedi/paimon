"""Choosing which attempts a person should label, and scoring the judge against them.

The judge decides one thing — does this response answer the question or decline
it — and until somebody checks it, that decision is a figure rather than a
measurement (ADR-0032). This module picks the sample to check and compares the
two raters afterwards.

**Stratified, not random.** A simple random sample of 450 attempts would be four
fifths answerable tasks, because that is what the dataset is, and the question
the judge exists to settle lives almost entirely in the fifth that is not. Taking
a fixed share from every (system, category) cell buys labels where the
disagreement is, at the cost of a sample that does not mirror the population —
which is the right trade, because kappa is about the raters and not about the
dataset's shape.

**The labeller is not shown the judge's verdict.** Showing it is the documented
way to turn an independent measurement into an expensive confirmation of what
the model already said.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from paimon.evaluation.agent_benchmark import AgentReport
from paimon.evaluation.calibration import Agreement, HumanLabel, agreement
from paimon.evaluation.judging import Verdict

#: How the three parts of an attempt's identity are joined into a label's case
#: id. Chosen so a person reading the template can tell which run a row came
#: from, and so two systems' attempts at one task cannot collide.
SEPARATOR = "/"


def attempt_id(system: str, task_id: str, trial: int) -> str:
    """Identify one attempt across every report in a comparison."""
    return f"{system}{SEPARATOR}{task_id}{SEPARATOR}{trial}"


@dataclass(frozen=True, slots=True)
class SampledAttempt:
    """One attempt chosen for a person to classify."""

    case_id: str
    system: str
    category: str
    question: str
    answer: str
    judged: Verdict
    """What the judge said. Carried so the comparison can be made afterwards,
    and **never written into the template**."""


def stratified_sample(
    reports: Sequence[AgentReport], *, per_cell: int = 2, seed: int = 0
) -> list[SampledAttempt]:
    """Take a fixed number of attempts from every (system, category) cell.

    Args:
        reports: One per system, all over the same dataset.
        per_cell: Attempts to take from each cell. Two across three systems and
            five categories is thirty; four is sixty, which is the size a kappa
            over three labels starts to mean something at.
        seed: Which attempts are taken, for a sample somebody else can rebuild.
            Deterministic rather than random: a calibration sample that cannot
            be reproduced cannot be re-labelled by a second person, and a second
            labeller is the only way to know whether the first was unusual.

    Returns:
        The chosen attempts, ordered by system and category so a labeller works
        through one kind of thing at a time.
    """
    chosen: list[SampledAttempt] = []
    for report in reports:
        cells: dict[str, list[SampledAttempt]] = {}
        for task in report.tasks:
            for attempt, mark in zip(task.attempts, task.grades, strict=True):
                if attempt.failed:
                    # A crashed run is not a response, so there is nothing for a
                    # person to classify.
                    continue
                cells.setdefault(task.category, []).append(
                    SampledAttempt(
                        case_id=attempt_id(report.system, task.task_id, attempt.trial),
                        system=report.system,
                        category=task.category,
                        question=task.question,
                        answer=attempt.text,
                        judged=_verdict_of(mark.outcome),
                    )
                )
        for category in sorted(cells):
            found = cells[category]
            # Spread across the cell rather than taking the first few, which
            # would be the same low-numbered tasks in every system and would
            # measure the judge on a third of the corpus.
            step = max(len(found) // per_cell, 1)
            chosen.extend(found[seed % step :: step][:per_cell])
    return chosen


def _verdict_of(outcome: object) -> Verdict:
    """Read the judge's classification back as the verdict a person will give."""
    return Verdict.YES if str(outcome) == "refused" else Verdict.NO


def template(sample: Sequence[SampledAttempt]) -> str:
    """Render the blank file a person fills in.

    One attempt per line, carrying the question and the response and nothing
    else. No system name, no category, no judge verdict: each of the three would
    tell the labeller what to expect, and a labeller who has been told what to
    expect is confirming rather than measuring.
    """
    rows = [
        {
            "case_id": item.case_id,
            "question": item.question,
            "response": item.answer,
            "refusal": "",
        }
        for item in sample
    ]
    return "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"


def instructions() -> str:
    """What the labeller is being asked, in the words the judge was asked it."""
    return (
        "Fill in `refusal` on each line with one of:\n\n"
        '  "yes"     — the response declines. It says the sources, the documentation\n'
        "              or the corpus do not cover the question, or that it cannot\n"
        "              answer from what it was given.\n"
        '  "partial" — it declines the thing asked while giving related context.\n'
        '  "no"      — it answers. It states the fact, procedure or conclusion asked\n'
        "              for, however wrong that answer may be.\n\n"
        "Citations do not make a response an answer: judge the claim, not the\n"
        "markers. Do not decide whether the answer is correct, and do not guess\n"
        "whether the corpus ought to have covered the question.\n\n"
        "Leave a line blank to skip it. Blank lines are not counted against either\n"
        "rater.\n"
    )


def calibrate(sample: Sequence[SampledAttempt], labels: Sequence[HumanLabel]) -> Agreement:
    """Compare the judge's classifications against a person's.

    Args:
        sample: The attempts that were offered for labelling, carrying what the
            judge said about each.
        labels: What the person decided. Rows they left blank are absent.

    Returns:
        Raw agreement, Cohen's kappa and how many cases both rated.
    """
    theirs: Mapping[str, Verdict] = {
        label.case_id: label.refusal for label in labels if label.refusal is not None
    }
    mine = {item.case_id: item.judged for item in sample}
    return agreement(mine, theirs)


def write_template(sample: Sequence[SampledAttempt], path: Path) -> None:
    """Write the blank template and its instructions beside each other."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template(sample), encoding="utf-8")
    path.with_suffix(".md").write_text(
        f"# Labelling {len(sample)} responses\n\n{instructions()}", encoding="utf-8"
    )


__all__ = [
    "SampledAttempt",
    "attempt_id",
    "calibrate",
    "instructions",
    "stratified_sample",
    "template",
    "write_template",
]
