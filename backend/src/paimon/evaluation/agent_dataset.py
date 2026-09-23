"""The golden set for agents: open questions, and what a right outcome is.

Separate from :mod:`paimon.evaluation.dataset` for one reason that turns out to
matter a great deal. A retrieval case must name a passage that answers it —
``EvaluationCase`` refuses one that does not, because a question with no expected
passage scores as a failure for every configuration and measures nothing.

An agent set needs the opposite case as well. **Some questions the corpus cannot
answer, and the right outcome is a refusal.** Those tasks carry no supporting
passage by construction, and they are not an afterthought: Anthropic's guidance
on agent evaluation puts cases where the agent must *not* act alongside the ones
where it must, because a system that answers everything passes every test that
only asks whether it answered.

So the outcome is what is declared, not the route. How many tools a run called,
in what order, is reported and never scored (ADR-0046).
"""

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from paimon.evaluation.dataset import SupportingPassage


class Outcome(StrEnum):
    """What a system is supposed to do with a task.

    Two values and not three. "Answer if you can" is not a criterion — it is
    what every system does anyway, and a task that accepts either outcome
    measures nothing. Deciding which of the two applies is the work of writing
    the task, and it is done once, by a person, before the task ships.
    """

    ANSWER = "answer"
    """The corpus supports an answer. A system that refuses is wrong."""

    REFUSE = "refuse"
    """It does not. A system that answers is wrong, however fluent the answer."""


@dataclass(frozen=True, slots=True)
class AgentTask:
    """One question, and everything needed to decide whether it went well.

    Attributes:
        task_id: Identifier, stable across dataset revisions.
        question: What the system is asked.
        expected: The outcome a correct system reaches.
        category: What this task is testing — the report is broken down by it,
            because a system that is good at one hop and hopeless at two is two
            different facts and one average hides both.
        supporting: The passages an answer has to rest on, anchored to a
            document and a quotation rather than to a chunk id (ADR-0013).
        reference: The answer the author reached by hand, in prose. Not scored
            against; it exists so that a task nobody has actually answered
            cannot ship, and so a disputed grade can be settled by reading.
    """

    task_id: str
    question: str
    expected: Outcome
    category: str
    supporting: tuple[SupportingPassage, ...] = ()
    reference: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """Reject a task that cannot be scored, or that scores nothing.

        Raises:
            ValueError: If the task has no id, question or category; if an
                answerable task names no supporting passage; if a refusal task
                names one; or if it carries no reference solution.
        """
        for name in ("task_id", "question", "category"):
            if not str(getattr(self, name)).strip():
                msg = f"an agent task requires a non-empty {name}"
                raise ValueError(msg)
        if self.expected is Outcome.ANSWER and not self.supporting:
            msg = (
                f"task '{self.task_id}' expects an answer but names no supporting "
                "passage, so nothing decides whether the answer was right"
            )
            raise ValueError(msg)
        if self.expected is Outcome.REFUSE and self.supporting:
            msg = (
                f"task '{self.task_id}' expects a refusal and names supporting "
                "passages; if the corpus supports an answer, the expectation is wrong"
            )
            raise ValueError(msg)
        if not self.reference.strip():
            # The rule that keeps the set honest: a task the author never solved
            # is a task that grades every system identically and measures none.
            msg = f"task '{self.task_id}' has no reference solution, so nobody has solved it"
            raise ValueError(msg)

    @property
    def documents(self) -> tuple[str, ...]:
        """The documents this task's answer draws on, in order, without repeats."""
        return tuple(dict.fromkeys(passage.document_id for passage in self.supporting))


@dataclass(frozen=True, slots=True)
class AgentDataset:
    """A versioned set of agent tasks."""

    name: str
    tasks: tuple[AgentTask, ...]

    def __iter__(self) -> Iterator[AgentTask]:
        """Iterate the tasks in file order."""
        return iter(self.tasks)

    def __len__(self) -> int:
        """How many tasks the set holds."""
        return len(self.tasks)

    @property
    def categories(self) -> tuple[str, ...]:
        """Every category present, in first-seen order."""
        return tuple(dict.fromkeys(task.category for task in self.tasks))

    def by_category(self) -> Mapping[str, tuple[AgentTask, ...]]:
        """The tasks grouped by what they test."""
        grouped: dict[str, list[AgentTask]] = {}
        for task in self.tasks:
            grouped.setdefault(task.category, []).append(task)
        return {name: tuple(tasks) for name, tasks in grouped.items()}

    @classmethod
    def from_jsonl(cls, path: Path) -> "AgentDataset":
        """Load a dataset from a JSON Lines file.

        Args:
            path: The file to read. Blank lines and ``//`` comments are skipped,
                so the set can carry its own instructions at the top.

        Returns:
            The loaded dataset, named after the file.

        Raises:
            ValueError: If a line is not valid JSON, describes an unscoreable
                task, or repeats an id. Loading fails loudly: a benchmark that
                silently skips malformed tasks reports an improvement that is
                really an absence.
        """
        tasks: list[AgentTask] = []
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip() or line.lstrip().startswith("//"):
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as error:
                msg = f"{path.name} line {number} is not valid JSON: {error}"
                raise ValueError(msg) from error
            tasks.append(_task_from(raw, f"{path.name} line {number}"))

        seen = [task.task_id for task in tasks]
        repeated = sorted({item for item in seen if seen.count(item) > 1})
        if repeated:
            # Two tasks under one id make a paired comparison line up the wrong
            # rows, which is a wrong number with a confident interval around it.
            listed = ", ".join(repeated)
            msg = f"{path.name} uses these task ids more than once: {listed}"
            raise ValueError(msg)
        return cls(name=path.stem, tasks=tuple(tasks))


def _task_from(raw: object, where: str) -> AgentTask:
    if not isinstance(raw, dict):
        msg = f"{where} is not an object"
        raise ValueError(msg)
    supporting_raw = raw.get("supporting") or []
    if not isinstance(supporting_raw, Sequence) or isinstance(supporting_raw, str):
        msg = f"{where}: 'supporting' must be a list"
        raise ValueError(msg)
    try:
        expected = Outcome(str(raw["expected"]))
    except (KeyError, ValueError) as error:
        allowed = ", ".join(outcome.value for outcome in Outcome)
        msg = f"{where}: 'expected' must be one of {allowed}"
        raise ValueError(msg) from error
    try:
        return AgentTask(
            task_id=str(raw["id"]),
            question=str(raw["question"]),
            expected=expected,
            category=str(raw["category"]),
            supporting=tuple(
                SupportingPassage(document_id=str(item["document_id"]), quote=str(item["quote"]))
                for item in supporting_raw
            ),
            reference=str(raw.get("reference", "")),
            tags=tuple(str(tag) for tag in raw.get("tags", ())),
        )
    except (KeyError, TypeError) as error:
        msg = f"{where}: {error}"
        raise ValueError(msg) from error


__all__ = ["AgentDataset", "AgentTask", "Outcome"]
