"""Structure-aware chunking.

Splits a document along its heading structure and packs the resulting blocks into
token-budgeted chunks. Two properties are maintained throughout, and both are
tested as invariants rather than assumed:

1. Every chunk's text is exactly ``document.text[start_char:end_char]``. A chunk
   that has been reworded, trimmed or re-joined cannot be cited, because the
   offsets no longer point at what the chunk says.
2. A chunk never spans two different heading paths. Retrieval returns the chunk
   whole, so a chunk straddling "Rollback procedure" and "Known issues" hands the
   model two contexts and lets it attribute a claim to the wrong one.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from paimon.domain.entities import Chunk, Document
from paimon.domain.ports import TokenCounter

_HEADING = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>.+?)\s*#*\s*$")
_SENTENCE = re.compile(r"[^.!?]*[.!?]+(?:\s+|$)|[^.!?]+$")
_WORD = re.compile(r"\S+\s*")

# Overlap needs at least one block to carry and one to keep, or a chunk could
# be built entirely from carried content and make no forward progress.
_MIN_BLOCKS_FOR_OVERLAP = 2


@dataclass(frozen=True, slots=True)
class ChunkingPolicy:
    """How a document is cut up.

    Attributes:
        max_tokens: Upper bound per chunk. Above it, retrieval returns more
            context than the answer needs and the prompt budget goes on padding.
        overlap_tokens: How much of the previous chunk is repeated at the start of
            the next, so a passage split across a boundary is still retrievable
            from either side.
        min_tokens: Chunks smaller than this are dropped. Their usual source is a
            heading with no body, which is noise in an index: it matches on title
            words and then carries no answer.
    """

    max_tokens: int = 512
    overlap_tokens: int = 64
    min_tokens: int = 16

    def __post_init__(self) -> None:
        """Reject a policy that cannot terminate or cannot produce a chunk.

        Raises:
            ValueError: If the budget is not positive, the overlap is negative or
                does not leave room for new content, or the minimum exceeds the
                maximum.
        """
        if self.max_tokens <= 0:
            msg = "max_tokens must be positive"
            raise ValueError(msg)
        if self.overlap_tokens < 0:
            msg = "overlap_tokens cannot be negative"
            raise ValueError(msg)
        if self.overlap_tokens >= self.max_tokens:
            msg = "overlap_tokens must leave room for new content within max_tokens"
            raise ValueError(msg)
        if self.min_tokens > self.max_tokens:
            msg = "min_tokens cannot exceed max_tokens"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class _Block:
    """A contiguous span of the document under one heading path."""

    start: int
    end: int
    heading_path: tuple[str, ...]
    tokens: int


def _split_into_blocks(text: str, count: TokenCounter) -> list[_Block]:
    """Cut the text into heading and paragraph blocks, tracking offsets."""
    blocks: list[_Block] = []
    stack: list[str] = []
    offset = 0
    pending_start: int | None = None
    pending_end = 0

    def flush() -> None:
        nonlocal pending_start
        if pending_start is None:
            return
        span = text[pending_start:pending_end]
        if span.strip():
            blocks.append(
                _Block(
                    start=pending_start,
                    end=pending_end,
                    heading_path=tuple(stack),
                    tokens=count.count(span),
                )
            )
        pending_start = None

    for line in text.splitlines(keepends=True):
        line_start = offset
        offset += len(line)
        stripped = line.strip()
        heading = _HEADING.match(stripped)

        if heading:
            flush()
            level = len(heading.group("hashes"))
            del stack[level - 1 :]
            stack.append(heading.group("title"))
            blocks.append(
                _Block(
                    start=line_start,
                    end=offset,
                    heading_path=tuple(stack),
                    tokens=count.count(line),
                )
            )
            continue

        if not stripped:
            flush()
            continue

        if pending_start is None:
            pending_start = line_start
        pending_end = offset

    flush()
    return blocks


def _split_oversized(text: str, block: _Block, budget: int, count: TokenCounter) -> list[_Block]:
    """Break a block that exceeds the budget on its own, keeping offsets exact.

    Sentences first, because a sentence boundary is the least damaging place to
    cut. Only a single sentence over budget falls back to splitting on words,
    which is a last resort rather than the strategy.
    """
    if block.tokens <= budget:
        return [block]

    pieces: list[_Block] = []
    for pattern in (_SENTENCE, _WORD):
        pieces = []
        for match in pattern.finditer(text[block.start : block.end]):
            if not match.group().strip():
                continue
            start = block.start + match.start()
            end = block.start + match.end()
            pieces.append(
                _Block(
                    start=start,
                    end=end,
                    heading_path=block.heading_path,
                    tokens=count.count(text[start:end]),
                )
            )
        if pieces and all(piece.tokens <= budget for piece in pieces):
            return pieces

    # Even single words exceed the budget, which means the budget is smaller than
    # the text's granularity. Returning the pieces is more useful than failing.
    return pieces or [block]


def _pack(blocks: Sequence[_Block], policy: ChunkingPolicy) -> list[list[_Block]]:
    """Group blocks of one section into chunks, applying the overlap."""
    groups: list[list[_Block]] = []
    current: list[_Block] = []
    current_tokens = 0

    for block in blocks:
        if current and current_tokens + block.tokens > policy.max_tokens:
            groups.append(current)
            carried = _carry_over(current, policy)
            current = [*carried, block]
            current_tokens = sum(item.tokens for item in current)
            continue
        current.append(block)
        current_tokens += block.tokens

    if current:
        groups.append(current)
    return groups


def _carry_over(previous: list[_Block], policy: ChunkingPolicy) -> list[_Block]:
    """Trailing blocks of the previous chunk to repeat at the start of the next.

    Never the whole previous chunk: an overlap that consumes everything would let
    a chunk make no forward progress, and the loop would not terminate.
    """
    if policy.overlap_tokens == 0 or len(previous) < _MIN_BLOCKS_FOR_OVERLAP:
        return []
    carried: list[_Block] = []
    budget = policy.overlap_tokens
    for block in reversed(previous[1:]):
        if block.tokens > budget:
            break
        carried.insert(0, block)
        budget -= block.tokens
    return carried


def chunk_document(document: Document, policy: ChunkingPolicy, count: TokenCounter) -> list[Chunk]:
    """Split a document into retrievable chunks.

    Args:
        document: The parsed, normalized document.
        policy: Token budget, overlap and minimum size.
        count: How tokens are counted.

    Returns:
        Chunks in document order, numbered from zero. Each chunk's text is a
        verbatim slice of the document.
    """
    blocks = _split_into_blocks(document.text, count)

    sized: list[_Block] = []
    for block in blocks:
        sized.extend(_split_oversized(document.text, block, policy.max_tokens, count))

    chunks: list[Chunk] = []
    ordinal = 0
    section: list[_Block] = []

    def close_section() -> None:
        nonlocal ordinal, section
        for group in _pack(section, policy):
            start = group[0].start
            end = group[-1].end
            text = document.text[start:end]
            tokens = count.count(text)
            if tokens < policy.min_tokens:
                continue
            chunks.append(
                Chunk(
                    chunk_id=f"{document.document_id}:{ordinal}",
                    document_id=document.document_id,
                    tenant_id=document.tenant_id,
                    ordinal=ordinal,
                    text=text,
                    start_char=start,
                    end_char=end,
                    token_count=tokens,
                    heading_path=group[0].heading_path,
                )
            )
            ordinal += 1
        section = []

    for block in sized:
        if section and block.heading_path != section[-1].heading_path:
            close_section()
        section.append(block)
    close_section()

    return chunks


class Chunker:
    """Splits documents according to a fixed policy.

    Bundles the policy and the token counter with the operation they configure,
    so callers ask for chunks rather than carrying the settings needed to produce
    them.
    """

    def __init__(self, policy: ChunkingPolicy, count: TokenCounter) -> None:
        """Initialise the chunker.

        Args:
            policy: Token budget, overlap and minimum size.
            count: How tokens are counted.
        """
        self._policy = policy
        self._count = count

    @property
    def policy(self) -> ChunkingPolicy:
        """The policy in force."""
        return self._policy

    @property
    def fingerprint(self) -> str:
        """A stable identifier for this policy.

        Part of what decides whether a document needs reindexing: the same bytes
        chunked at two different sizes are two different indexes.
        """
        return (
            f"chunk:max={self._policy.max_tokens}"
            f",overlap={self._policy.overlap_tokens}"
            f",min={self._policy.min_tokens}"
        )

    def split(self, document: Document) -> list[Chunk]:
        """Split a document into retrievable chunks.

        Args:
            document: The parsed, normalized document.

        Returns:
            Chunks in document order, numbered from zero.
        """
        return chunk_document(document, self._policy, self._count)
