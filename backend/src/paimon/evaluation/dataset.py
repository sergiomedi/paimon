"""The golden set: questions and the passages that answer them."""

import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Collapse whitespace so a quote matches regardless of wrapping.

    Chunk boundaries and Markdown reflowing change line breaks without changing
    the words, and a ground truth that breaks when a paragraph is rewrapped is a
    ground truth nobody maintains.
    """
    return _WHITESPACE.sub(" ", text).strip().casefold()


@dataclass(frozen=True, slots=True)
class SupportingPassage:
    """A passage that must be retrieved for a question to count as answered.

    Anchored to a document and a quotation, never to a chunk id (ADR-0013).
    Chunk ids depend on the chunking policy, so ground truth expressed in them
    would have to be rewritten every time the chunk size changed — which is
    precisely the experiment the benchmark exists to run.
    """

    document_id: str
    quote: str

    def is_supported_by(self, document_id: str, text: str) -> bool:
        """Whether a retrieved chunk contains this passage.

        Args:
            document_id: The retrieved chunk's document.
            text: The retrieved chunk's text.
        """
        return document_id == self.document_id and normalize(self.quote) in normalize(text)


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    """One question and everything needed to judge an answer to it."""

    case_id: str
    question: str
    supporting: tuple[SupportingPassage, ...]
    notes: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """Reject a case that cannot be scored.

        Raises:
            ValueError: If the case has no id, no question or no supporting
                passage. A question with no expected passage cannot be right or
                wrong, and silently scores as a failure for every configuration.
        """
        if not self.case_id.strip():
            msg = "an evaluation case requires an id"
            raise ValueError(msg)
        if not self.question.strip():
            msg = f"case '{self.case_id}' has no question"
            raise ValueError(msg)
        if not self.supporting:
            msg = f"case '{self.case_id}' has no supporting passage, so it cannot be scored"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class EvaluationDataset:
    """A versioned set of cases."""

    name: str
    cases: tuple[EvaluationCase, ...]

    def __iter__(self) -> Iterator[EvaluationCase]:
        """Iterate the cases in file order."""
        return iter(self.cases)

    def __len__(self) -> int:
        """How many cases the dataset holds."""
        return len(self.cases)

    @classmethod
    def from_jsonl(cls, path: Path) -> "EvaluationDataset":
        """Load a dataset from a JSON Lines file.

        One case per line, so a diff shows exactly which questions changed —
        which matters when the dataset is the thing every measurement is
        relative to.

        Args:
            path: The file to read.

        Returns:
            The loaded dataset, named after the file.

        Raises:
            ValueError: If a line is not valid JSON or describes an unscoreable
                case. Loading fails loudly: a benchmark that silently skips
                malformed cases reports an improvement that is really an absence.
        """
        cases: list[EvaluationCase] = []
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip() or line.lstrip().startswith("//"):
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as error:
                msg = f"{path.name} line {number} is not valid JSON: {error}"
                raise ValueError(msg) from error
            cases.append(_case_from(raw, f"{path.name} line {number}"))
        return cls(name=path.stem, cases=tuple(cases))


def _case_from(raw: object, where: str) -> EvaluationCase:
    if not isinstance(raw, dict):
        msg = f"{where} is not an object"
        raise ValueError(msg)
    supporting_raw = raw.get("supporting") or []
    if not isinstance(supporting_raw, Sequence) or isinstance(supporting_raw, str):
        msg = f"{where}: 'supporting' must be a list"
        raise ValueError(msg)
    try:
        supporting = tuple(
            SupportingPassage(document_id=str(item["document_id"]), quote=str(item["quote"]))
            for item in supporting_raw
        )
        return EvaluationCase(
            case_id=str(raw["id"]),
            question=str(raw["question"]),
            supporting=supporting,
            notes=str(raw.get("notes", "")),
            tags=tuple(str(tag) for tag in raw.get("tags", ())),
        )
    except (KeyError, TypeError) as error:
        msg = f"{where}: {error}"
        raise ValueError(msg) from error
