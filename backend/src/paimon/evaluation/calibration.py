"""Measuring how much this judge agrees with a person.

Everything in ADR-0031 narrows the ways a judge can go wrong. None of it makes it
agree with a human being more often, and the published agreement figures are
about *other* judges on *other* corpora with *other* rubrics. They say a judge of
this kind can be 56% accurate. They say nothing about what ours is worth here.

So this measures it. A person labels a sample of the same answers, and the report
gains a section saying how often the judge and the person reached the same
verdict — and, more usefully, Cohen's kappa, which discounts the agreement two
raters would reach by chance alone. Raw agreement flatters any rater on a skewed
dataset: if nine answers in ten are faithful, a judge that says "yes" to
everything agrees 90% of the time and has measured nothing.

Until that exists, the judged numbers are labelled **uncalibrated**, because a
judge whose agreement with a person is unknown produces a figure, not a
measurement.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from paimon.evaluation.judging import Verdict
from paimon.evaluation.statistics import Estimate, estimate

#: Industry practice alerts below this. Not a law of nature — it is a convention
#: for "the raters are measuring the same thing" — but a convention is better
#: than each project inventing its own threshold after seeing its numbers.
ACCEPTABLE_KAPPA = 0.6

#: The labels a person may give. The same three the judge chooses between, so the
#: two are directly comparable; "undecided" is not offered, because a person who
#: cannot decide should leave the case out rather than record a shrug.
LABELS = (Verdict.YES, Verdict.PARTIAL, Verdict.NO)


@dataclass(frozen=True, slots=True)
class HumanLabel:
    """One case as a person judged it."""

    case_id: str
    faithfulness: Verdict
    relevance: Verdict
    note: str = ""


@dataclass(frozen=True, slots=True)
class Agreement:
    """How closely two raters agreed on one question.

    Attributes:
        compared: Cases where both raters gave a usable verdict.
        raw: Fraction on which they matched exactly, with its interval. Reported
            because it is what people ask for, and never on its own.
        kappa: Cohen's kappa. Agreement above what chance would produce, from
            zero (no better than guessing) to one (perfect). Negative means worse
            than chance, which happens and is worth seeing.
        skipped: Cases the judge abstained on, or the person did not label.
    """

    compared: int
    raw: Estimate
    kappa: float
    skipped: int

    @property
    def is_acceptable(self) -> bool:
        """Whether the two raters look like they are measuring the same thing."""
        return self.compared > 0 and self.kappa >= ACCEPTABLE_KAPPA

    def format(self) -> str:
        """Render for a terminal."""
        if not self.compared:
            return "no cases could be compared"
        verdict = "acceptable" if self.is_acceptable else "TOO LOW"
        return (
            f"kappa {self.kappa:+.2f} ({verdict}), raw {self.raw.format(percent=True)}, "
            f"{self.compared} compared, {self.skipped} skipped"
        )


@dataclass(frozen=True, slots=True)
class Calibration:
    """What a person's labels said about this judge."""

    judge_model: str
    labels: int
    faithfulness: Agreement
    relevance: Agreement

    @property
    def is_acceptable(self) -> bool:
        """Whether both questions cleared the threshold."""
        return self.faithfulness.is_acceptable and self.relevance.is_acceptable


def cohens_kappa(first: Sequence[Verdict], second: Sequence[Verdict]) -> float:
    """Agreement between two raters, discounted for chance.

    ``(observed - expected) / (1 - expected)``, where expected is the agreement
    two raters would reach by chance given how often each uses each label.

    Returns:
        The kappa. One when both raters always agree. Zero when they agree no
        more than their label frequencies would predict. Undefined when chance
        agreement is already total — both raters used one label and the same one
        — and reported as 1.0 there, since they did in fact agree on everything.

    Raises:
        ValueError: If the sequences differ in length.
    """
    if len(first) != len(second):
        msg = f"kappa needs the same cases: {len(first)} against {len(second)}"
        raise ValueError(msg)
    n = len(first)
    if n == 0:
        return 0.0

    observed = sum(1 for a, b in zip(first, second, strict=True) if a is b) / n
    expected = sum((first.count(label) / n) * (second.count(label) / n) for label in LABELS)
    if expected >= 1.0:
        return 1.0
    return (observed - expected) / (1.0 - expected)


def agreement(judge: Mapping[str, Verdict], human: Mapping[str, Verdict]) -> Agreement:
    """Compare a judge's verdicts against a person's, case by case.

    Cases the judge abstained on are skipped rather than counted as
    disagreements: "the judge broke" and "the judge was wrong" are different
    facts, and only one of them is about its accuracy.
    """
    shared = [case_id for case_id in human if case_id in judge]
    usable = [case_id for case_id in shared if judge[case_id] is not Verdict.UNDECIDED]
    if not usable:
        return Agreement(compared=0, raw=estimate([]), kappa=0.0, skipped=len(shared))

    theirs = [judge[case_id] for case_id in usable]
    ours = [human[case_id] for case_id in usable]
    matches = [1.0 if a is b else 0.0 for a, b in zip(theirs, ours, strict=True)]
    return Agreement(
        compared=len(usable),
        raw=estimate(matches),
        kappa=cohens_kappa(theirs, ours),
        skipped=len(shared) - len(usable),
    )


def labelling_template(rows: Sequence[Mapping[str, object]]) -> str:
    """Render a file for a person to fill in, one case per line.

    JSON Lines rather than a spreadsheet, because it diffs, and one case per line
    so a half-finished file is still usable — the loader skips blank rows.

    The judge's own verdict is **not** included. Showing it would anchor the
    labeller to it, which is the well-documented way to turn an independent
    measurement into an expensive confirmation of what the model already said.
    """
    return "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"


def load_labels(path: Path) -> list[HumanLabel]:
    """Read a labelled file, refusing anything that is not a decision.

    Raises:
        ValueError: If a line is malformed, or carries a verdict that is not one
            of the three offered. A blank or unrecognised label is a case that
            was not judged, and silently treating it as "no" would put a person's
            unfinished work into a published number.
    """
    labels: list[HumanLabel] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            msg = f"{path}:{number} is not valid JSON: {error}"
            raise ValueError(msg) from error
        if not isinstance(raw, dict):
            msg = f"{path}:{number} is not an object"
            raise ValueError(msg)
        if not raw.get("faithfulness") and not raw.get("relevance"):
            # An unlabelled row, left in the template. Skipped rather than
            # refused, so a partly finished file still measures what it covers.
            continue
        labels.append(
            HumanLabel(
                case_id=str(raw["case_id"]),
                faithfulness=_verdict(raw, "faithfulness", f"{path}:{number}"),
                relevance=_verdict(raw, "relevance", f"{path}:{number}"),
                note=str(raw.get("note", "")),
            )
        )
    return labels


def _verdict(raw: Mapping[str, object], field: str, where: str) -> Verdict:
    """Read one label, or say which line is wrong."""
    value = str(raw.get(field, "")).strip().lower()
    try:
        verdict = Verdict(value)
    except ValueError:
        allowed = ", ".join(label.value for label in LABELS)
        msg = f"{where} has {field}={value!r}; use one of: {allowed}"
        raise ValueError(msg) from None
    if verdict is Verdict.UNDECIDED:
        msg = (
            f"{where} has {field}='undecided'. A person who cannot decide should leave "
            "the case blank rather than record a shrug — a blank row is skipped, and an "
            "undecided one would be counted."
        )
        raise ValueError(msg)
    return verdict


__all__ = [
    "ACCEPTABLE_KAPPA",
    "LABELS",
    "Agreement",
    "Calibration",
    "HumanLabel",
    "agreement",
    "cohens_kappa",
    "labelling_template",
    "load_labels",
]
