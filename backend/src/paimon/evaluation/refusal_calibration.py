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

**Distinct texts, and no canned refusals.** At temperature zero the same task
produces the same answer five times, so a sample drawn from attempts is mostly
repeats: a person labels the same paragraph over and over and kappa counts one
judgement five times. And the platform's own refusal sentences are graded by
code, by equality against the constants that define them, so a judge classifying
them is measured on work it was never given — and a labeller who recognises one
knows which harness produced it.

**The labeller is shown the response and nothing else** — not the question, not
the judge's verdict, not which system produced it. The verdict would anchor
them, which is the documented way to turn an independent measurement into an
expensive confirmation. Withholding the *question* is the stronger requirement:
kappa compares two raters, which means nothing unless they were asked the same
thing, and the judge is shown the text alone.
"""

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import Any

from paimon.evaluation.calibration import Agreement, HumanLabel, agreement
from paimon.evaluation.judging import REFUSAL_QUESTION, Verdict

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


def distinct_texts(attempts: Sequence[SampledAttempt]) -> list[SampledAttempt]:
    """Keep one attempt per distinct response, in the order they appear.

    At temperature zero a system answers the same task the same way five times,
    so a sample of sixty attempts held forty-one distinct texts. The twenty-nine
    repeats cost a person their attention and buy no information: kappa over a
    text labelled five times counts one judgement five times and reports an
    agreement narrower than the evidence supports.
    """
    seen: set[str] = set()
    kept: list[SampledAttempt] = []
    for item in attempts:
        fingerprint = " ".join(item.answer.split())
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        kept.append(item)
    return kept


def without_canned_refusals(
    attempts: Sequence[SampledAttempt], canned: Collection[str]
) -> list[SampledAttempt]:
    """Drop the responses the platform wrote rather than a model.

    Two reasons, and the second is the stronger one. Code already grades these
    exactly — they are matched by equality against the constants that define
    them — so a judge classifying them is being measured on the one part of the
    job it was never given. And they give the system away: only the two agents
    emit them, so a labeller who recognises one knows which harness produced it
    and is no longer rating the text alone.
    """
    exact = {" ".join(text.split()) for text in canned}
    return [item for item in attempts if " ".join(item.answer.split()) not in exact]


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
        size: Trim to this many. Only the strata fill is trimmed — the named
            cases always survive, or the trim would quietly undo them.
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

    wanted = set(must_include)
    required = [item for item in chosen.values() if item.case_id in wanted]
    rest = [item for item in chosen.values() if item.case_id not in wanted]

    # Seeded, not secure: this decides which cases a person reads and in what
    # order, and being reproducible matters here while being unguessable does
    # not.
    shuffle = Random(seed).shuffle  # noqa: S311
    # Shuffled *before* the trim, not after. The cells are visited in sorted
    # order, so trimming the fill as it stands drops whole systems off the end
    # — the first version left three investigator rows in sixty, having filled
    # the budget alphabetically from "answers".
    shuffle(rest)
    if size is not None:
        # Trim the fill, never the required. A sample that dropped the cases it
        # was built around would measure agreement on the easy ones and say
        # nothing about the contested ones, which is the whole reason they were
        # named.
        rest = rest[: max(size - len(required), 0)]

    picked = required + rest
    shuffle(picked)
    return picked


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
    """What the labeller is asked — the same words the judge is asked.

    Rendered from :data:`~paimon.evaluation.judging.REFUSAL_QUESTION` rather
    than written out, because writing it twice is what produced the one
    difference that mattered: the judge never received "a confidently wrong
    answer is an answer", and duly graded a fabricated answer as a refusal.

    Only the mechanics differ, and they have to. The judge replies in JSON and
    is pushed off the fence; a person fills in a field and may leave a row
    blank. A blank costs one case from the comparison, which is cheaper than a
    guess recorded as a judgement.
    """
    return (
        f"{REFUSAL_QUESTION}\n\n"
        "Fill in `refusal` on each line with `yes` or `no`.\n\n"
        "Leave a line blank to skip it. Blank lines are not counted against either\n"
        "rater — better a gap than a guess.\n"
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
    "without_canned_refusals",
    "write_template",
]
