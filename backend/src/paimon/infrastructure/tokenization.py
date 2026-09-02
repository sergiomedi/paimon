"""Token counting."""

import re

_TOKEN = re.compile(r"\w+|[^\w\s]")

# Sub-word tokenizers split long or unusual words into several tokens, so a
# word count understates the true figure. This factor is a deliberate
# over-estimate: chunking that overshoots the budget produces chunks slightly
# too small, while undershooting produces prompts that are rejected.
_SUBWORD_FACTOR = 1.3


class HeuristicTokenCounter:
    """Approximates token counts without a model-specific vocabulary.

    Exact counting needs the tokenizer of the model in use, which means either a
    network fetch of BPE files at runtime or a large vendored vocabulary — the
    first breaks offline development and CI, the second is weight this phase does
    not need. The approximation is consistent, which is what chunk sizing
    actually requires, and it is deliberately biased to over-count.

    When exact counts start to matter — prompt budgeting close to a model's
    context limit, or cost estimation — a model-accurate counter replaces this one
    through the TokenCounter port, and no chunking logic changes.
    """

    def count(self, text: str) -> int:
        """Approximate the number of tokens in the text.

        Args:
            text: Text to measure.

        Returns:
            An estimate, never below zero and never below one for non-empty text.
        """
        if not text.strip():
            return 0
        units = len(_TOKEN.findall(text))
        return max(1, round(units * _SUBWORD_FACTOR))
