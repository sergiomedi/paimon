"""Port for counting tokens."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class TokenCounter(Protocol):
    """Counts tokens in text.

    Chunking needs a token budget, and prompt assembly needs one too. Both are
    expressed against this port rather than a specific tokenizer, because the
    right tokenizer depends on the model in use: an OpenAI BPE for Azure, a
    SentencePiece vocabulary for a local model. Injecting it keeps chunking free
    of that choice, and lets an approximate counter be replaced by an exact one
    without touching a line of chunking logic.
    """

    def count(self, text: str) -> int:
        """Number of tokens in the text.

        Args:
            text: Text to measure.
        """
        ...
