"""Assembling the prompt that turns retrieved chunks into a grounded answer."""

from collections.abc import Sequence
from dataclasses import dataclass

from paimon.domain.entities import Chunk
from paimon.domain.ports import Message, TokenCounter

SYSTEM_PROMPT = """\
You answer questions about an engineering organization's operational documentation.

Rules, in order of importance:

1. Answer only from the numbered sources below. If they do not contain the answer, \
say so plainly and stop. Do not fall back on general knowledge — an answer that \
sounds right but is not in the sources is worse than no answer, because the reader \
cannot tell the difference.
2. Cite every claim with the marker of the source it came from, like [1] or [2][3]. \
A sentence without a marker is a sentence the reader cannot check.
3. Never cite a source number that is not in the list.
4. Prefer the exact wording of a procedure over a paraphrase of it. \
Steps that have been reworded have been changed.
5. If the sources disagree, say so and cite both.
"""

DEFAULT_CONTEXT_TOKENS = 6000


@dataclass(frozen=True, slots=True)
class PromptBundle:
    """A prompt, and the sources its markers refer to.

    The two travel together because the numbering is positional: source three is
    whatever ended up third in this list. Returning the messages without the
    sources they were built from would make the answer's markers unresolvable.
    """

    messages: tuple[Message, ...]
    sources: tuple[Chunk, ...]


def build_prompt(
    question: str,
    chunks: Sequence[Chunk],
    count: TokenCounter,
    max_context_tokens: int = DEFAULT_CONTEXT_TOKENS,
) -> PromptBundle:
    """Build the prompt for a grounded answer.

    Chunks are included in retrieval order until the context budget is spent, so
    the best-ranked material survives when there is not room for everything.
    Truncating the middle of a chunk is not an option: a half-quoted procedure is
    a procedure with steps missing, and the model has no way to know it.

    Args:
        question: The question, as asked.
        chunks: Retrieved chunks, best first.
        count: How tokens are counted.
        max_context_tokens: Budget for the sources section.

    Returns:
        The messages to send and the sources their markers refer to.
    """
    included: list[Chunk] = []
    rendered: list[str] = []
    spent = 0

    for chunk in chunks:
        block = _render(len(included) + 1, chunk)
        cost = count.count(block)
        if included and spent + cost > max_context_tokens:
            break
        included.append(chunk)
        rendered.append(block)
        spent += cost

    context = "\n\n".join(rendered) if rendered else "(no sources were retrieved)"
    user = f"Sources:\n\n{context}\n\nQuestion: {question}"

    return PromptBundle(
        messages=(
            Message(role="system", content=SYSTEM_PROMPT),
            Message(role="user", content=user),
        ),
        sources=tuple(included),
    )


def _render(marker: int, chunk: Chunk) -> str:
    """Render one numbered source block."""
    heading = f" — {chunk.heading_trail}" if chunk.heading_path else ""
    return f"[{marker}] {chunk.document_id}{heading}\n{chunk.text}"
