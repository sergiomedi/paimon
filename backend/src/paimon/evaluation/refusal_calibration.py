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

**The labeller is shown the response and nothing else** — not the question, not
the judge's verdict, not which system produced it. The verdict would anchor
them, which is the documented way to turn an independent measurement into an
expensive confirmation. Withholding the *question* is the stronger requirement:
kappa compares two raters, which means nothing unless they were asked the same
thing, and the judge is shown the text alone.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import Any

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
    judged: Verdict | None = None
    """What the judge said, when a judge has run. None when the sample was taken
    from raw transcripts, which is how a calibration sample should be taken:
    the verdict under test must not choose or annotate the cases that test it."""


def stratified_sample(
    attempts: Sequence[SampledAttempt],
    *,
    per_cell: int = 4,
    must_include: Sequence[str] = (),
    size: int | None = None,
    seed: int = 0,
) -> list[SampledAttempt]:
    """Take a fixed share from every (system, category) cell, plus named cases.

    Args:
        attempts: Every attempt available to sample from.
        per_cell: How many to take from each cell before the named ones.
        must_include: Case ids that must appear whatever the strata say — the
            attempts a grader and a person are known to disagree about. They go
            in **unmarked and shuffled among the rest**: a labeller who can tell
            which cases are contested will label those more carefully than the
            others, and a kappa computed over cases rated with two different
            levels of care measures the labeller's attention rather than the
            judge.
        size: Trim to this many after the strata and the named cases are in.
        seed: Which attempts are taken and in what order, so somebody else can
            rebuild exactly this sample and label it independently.

    Returns:
        The chosen attempts, shuffled deterministically.
    """
    by_id = {item.case_id: item for item in attempts}
    cells: dict[tuple[str, str], list[SampledAttempt]] = {}
    for item in attempts:
        cells.setdefault((item.system, item.category), []).append(item)

    chosen: dict[str, SampledAttempt] = {
        case_id: by_id[case_id] for case_id in must_include if case_id in by_id
    }
    for key in sorted(cells):
        found = cells[key]
        step = max(len(found) // per_cell, 1)
        for item in found[seed % step :: step][:per_cell]:
            chosen.setdefault(item.case_id, item)

    picked = list(chosen.values())
    # Seeded, not secure: this decides reading order for a person, and being
    # reproducible matters here while being unguessable does not.
    Random(seed).shuffle(picked)  # noqa: S311
    return picked[:size] if size is not None else picked


def attempts_from_report(raw: Mapping[str, Any]) -> list[SampledAttempt]:
    """Read every attempt out of a report, judged or not.

    Works on a report written before any judge ran, which is the point: the
    sample a person labels must not be selected by, or annotated with, the
    verdict it exists to check.
    """
    system = str(raw.get("system", "unknown"))
    found: list[SampledAttempt] = []
    for task in raw.get("tasks", ()):
        for attempt in task.get("attempts", ()):
            if attempt.get("failed"):
                continue
            found.append(
                SampledAttempt(
                    case_id=attempt_id(system, str(task["task_id"]), int(attempt["trial"])),
                    system=system,
                    category=str(task.get("category", "")),
                    question=str(task.get("question", "")),
                    answer=str(attempt.get("text", "")),
                    judged=None,
                )
            )
    return found


def opaque_ids(sample: Sequence[SampledAttempt]) -> dict[str, str]:
    """Map each sampled attempt to a label that says nothing about it.

    The real id is ``system/task/trial`` — readable on purpose, and unusable in
    a labelling file. It names the system, which tells a labeller which harness
    they are rating, and it names the task, which tells anyone who has seen the
    dataset what category it is: whoever wrote it knows that a023 to a027 are
    the questions the corpus cannot answer, and a labeller who knows a refusal
    is expected will find one.

    So the template carries ``c01``, ``c02``, and the mapping stays with the
    benchmark rather than with the labeller.
    """
    return {item.case_id: f"c{index:02d}" for index, item in enumerate(sample, start=1)}


def template(sample: Sequence[SampledAttempt], labels: Mapping[str, str] | None = None) -> str:
    """Render the blank file a person fills in.

    One attempt per line, carrying **the response and nothing else** — not the
    question, not the system, not the category, not the judge's verdict, and
    not an id that gives any of them away (see :func:`opaque_ids`).

    Withholding the question is not caution, it is the requirement. Kappa
    measures whether two raters agree, which is only meaningful when they were
    asked the same thing, and the judge is shown the text alone. A person
    holding the question would be answering a harder question — did this answer
    it *well* — which is the question that made the judge call a correct
    one-sentence answer a refusal.
    """
    naming = labels or opaque_ids(sample)
    rows = [
        {"case_id": naming[item.case_id], "response": item.answer, "refusal": ""} for item in sample
    ]
    return "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"


def instructions() -> str:
    """What the labeller is being asked, in the words the judge was asked it."""
    return (
        "You are given pieces of text. For each, decide one thing: does it answer\n"
        "a question, or does it decline to?\n\n"
        "You are not shown the question. That is deliberate — the judge being\n"
        "measured is not shown it either, and an agreement figure between two\n"
        "raters answering different questions measures nothing.\n\n"
        "Fill in `refusal` on each line with one of:\n\n"
        '  "yes" — it declines. It says the sources, the documentation or the\n'
        "          corpus do not cover what was asked, or that it cannot answer\n"
        "          from what it was given.\n"
        '  "no"  — it answers. It states a fact, a procedure, a conclusion or a\n'
        "          recommendation.\n\n"
        "There are two labels and no third.\n\n"
        "  - Citations do not make a text an answer. A refusal that lists the\n"
        "    sources it looked at is still a refusal.\n"
        "  - A short answer is an answer. Brevity is not declining.\n"
        "  - A hedged answer is an answer.\n"
        "  - Text you suspect is incomplete is still an answer: you cannot see the\n"
        "    question, so you cannot know what would have been complete.\n"
        "  - Do not decide whether the answer is correct. A confidently wrong\n"
        "    answer is an answer.\n\n"
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
    naming = opaque_ids(sample)
    back = {label: case_id for case_id, label in naming.items()}
    theirs: Mapping[str, Verdict] = {
        back.get(label.case_id, label.case_id): label.refusal
        for label in labels
        if label.refusal is not None
    }
    mine = {item.case_id: item.judged for item in sample if item.judged is not None}
    return agreement(mine, theirs)


def write_template(sample: Sequence[SampledAttempt], path: Path, key: Path | None = None) -> None:
    """Write the blank template, its instructions, and the key that maps it back.

    The key is written **somewhere else** — it is what turns ``c17`` back into
    ``answers/a001/3``, and a labeller holding it is a labeller who can look up
    which system and which task each row came from.
    """
    naming = opaque_ids(sample)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template(sample, naming), encoding="utf-8")
    path.with_suffix(".md").write_text(
        f"# Labelling {len(sample)} responses\n\n{instructions()}", encoding="utf-8"
    )
    if key is not None:
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_text(
            "\n".join(
                json.dumps({"label": label, "case_id": case_id}, ensure_ascii=False)
                for case_id, label in naming.items()
            )
            + "\n",
            encoding="utf-8",
        )


__all__ = [
    "SampledAttempt",
    "attempt_id",
    "attempts_from_report",
    "calibrate",
    "instructions",
    "opaque_ids",
    "stratified_sample",
    "template",
    "write_template",
]
