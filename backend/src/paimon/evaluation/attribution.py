"""Checking that an answer is anchored where it says it is.

Most RAG evaluations ask a language model whether an answer is grounded. This
one does not have to, and that is a consequence of a decision made two phases
ago rather than a cleverness here: every citation this platform produces carries
a ``document_id``, a ``start_char`` and an ``end_char`` (ADR-0013). So "is this
answer grounded?" is not a judgement. It is a lookup — open the document, go to
the offset, and see whether the quoted text is there.

The difference matters more than it sounds. The TREC 2024 RAG track compared
GPT-4o against human assessors on exactly this question — does a citation support
a claim — and they agreed **56%** of the time on a three-level scale. The model
also systematically **over-credited** support: where a human said "no support",
it tended to say "partial". A judge that is wrong in a known direction on the
question of whether an answer is made up is the worst possible judge to have.

So: verify what can be verified, judge only what cannot, and say in the report
which is which. What this module checks is **attribution** — that the citations
point at real text that really says what the answer claims it says. Whether the
prose is a fair reading of that text is a separate question, and the next batch
asks a model about it, knowing what that answer is worth.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from paimon.domain.value_objects import Citation
from paimon.evaluation.dataset import normalize

#: A sentence that carries no marker. Not automatically a failure — "I could not
#: find anything about this" is a sentence and should not be cited — but a
#: sentence making a claim without one is the thing this platform exists not to
#: do.
MARKER = re.compile(r"\[(\d+)\]")

#: Sentence boundaries, roughly. Deliberately simple: a full sentence splitter
#: brings a model or a large dependency, and the failure mode here is splitting
#: one sentence into two, which costs a marker check that a correct answer
#: passes anyway.
SENTENCE = re.compile(r"(?<=[.!?])\s+")


class Attribution(StrEnum):
    """What happened when a citation was checked against its source."""

    RESOLVED = "resolved"
    """The offsets point at text, and that text contains the quote."""

    MISQUOTED = "misquoted"
    """The offsets point at text, and it is not what the citation claims."""

    OUT_OF_RANGE = "out_of_range"
    """The offsets fall outside the document. Nothing is there to check."""

    UNKNOWN_DOCUMENT = "unknown_document"
    """The cited document does not exist in the corpus."""


@dataclass(frozen=True, slots=True)
class CitationCheck:
    """One citation, and whether it survives being followed."""

    marker: int
    document_id: str
    attribution: Attribution
    quote: str
    found: str = ""

    @property
    def ok(self) -> bool:
        """Whether this citation points at what it says it does."""
        return self.attribution is Attribution.RESOLVED


@dataclass(frozen=True, slots=True)
class AttributionReport:
    """Whether one answer is anchored where it claims to be.

    Attributes:
        checks: One per citation, in the order the answer used them.
        sentences: Sentences in the answer.
        uncited_sentences: Those carrying no marker at all.
        invented_markers: Markers in the text that no citation explains. The
            platform already strips these before answering, so a non-empty list
            here is a regression in that stripping rather than a model quirk.
    """

    checks: tuple[CitationCheck, ...]
    sentences: int
    uncited_sentences: tuple[str, ...]
    invented_markers: tuple[int, ...]

    @property
    def resolved(self) -> int:
        """Citations that pointed at what they claimed."""
        return sum(1 for check in self.checks if check.ok)

    @property
    def citation_accuracy(self) -> float:
        """Fraction of citations that survived being followed.

        An answer with no citations scores zero rather than one. The alternative
        reads better and says that an answer citing nothing is perfectly
        attributed, which is exactly backwards.
        """
        return self.resolved / len(self.checks) if self.checks else 0.0

    @property
    def cited_sentence_rate(self) -> float:
        """Fraction of sentences carrying at least one marker."""
        if not self.sentences:
            return 0.0
        return (self.sentences - len(self.uncited_sentences)) / self.sentences

    @property
    def is_fully_attributed(self) -> bool:
        """Whether every citation resolved and every sentence carried one.

        The strict reading, reported alongside the fractions rather than instead
        of them: an answer that is 90% attributed still contains a claim nobody
        can check.
        """
        return (
            bool(self.checks) and self.resolved == len(self.checks) and not self.uncited_sentences
        )


def check_citation(citation: Citation, documents: Mapping[str, str]) -> CitationCheck:
    """Follow one citation into the corpus.

    Args:
        citation: The citation to verify.
        documents: Document text by id, as it was indexed.

    Returns:
        What was found where the citation pointed.
    """
    text = documents.get(citation.document_id)
    if text is None:
        return CitationCheck(
            marker=citation.marker,
            document_id=citation.document_id,
            attribution=Attribution.UNKNOWN_DOCUMENT,
            quote=citation.quote,
        )
    if citation.end_char > len(text) or citation.start_char >= len(text):
        return CitationCheck(
            marker=citation.marker,
            document_id=citation.document_id,
            attribution=Attribution.OUT_OF_RANGE,
            quote=citation.quote,
        )

    span = text[citation.start_char : citation.end_char]
    # Whitespace-insensitively, for the same reason the golden set matches that
    # way: a chunker that normalised a line break has not invented a citation.
    resolved = normalize(citation.quote) in normalize(span)
    return CitationCheck(
        marker=citation.marker,
        document_id=citation.document_id,
        attribution=Attribution.RESOLVED if resolved else Attribution.MISQUOTED,
        quote=citation.quote,
        found=span,
    )


def check_answer(
    text: str, citations: Sequence[Citation], documents: Mapping[str, str]
) -> AttributionReport:
    """Verify an answer against the corpus it claims to rest on.

    Args:
        text: The answer as written.
        citations: What it cited.
        documents: Document text by id.

    Returns:
        Every citation followed, and which sentences carried no marker.
    """
    checks = tuple(check_citation(citation, documents) for citation in citations)
    sentences = [sentence for sentence in SENTENCE.split(text.strip()) if sentence.strip()]
    uncited = tuple(sentence for sentence in sentences if not MARKER.search(sentence))

    known = {citation.marker for citation in citations}
    used = {int(marker) for marker in MARKER.findall(text)}
    return AttributionReport(
        checks=checks,
        sentences=len(sentences),
        uncited_sentences=uncited,
        invented_markers=tuple(sorted(used - known)),
    )


__all__ = [
    "MARKER",
    "SENTENCE",
    "Attribution",
    "AttributionReport",
    "CitationCheck",
    "check_answer",
    "check_citation",
]
