"""What an HTTP provider said, when it said no.

Every adapter in this package talks to somebody else's API over HTTP, and every
one of them has to turn a failed response into a domain error. The status code
names a *category* of failure. The body names the *cause*, and the difference
between the two is the difference between an afternoon and a minute:

    embedding provider returned 404
    embedding provider returned 404 (model "bge-m3" not found, try pulling it first)

Both describe the same response. Only one of them says what to do about it. This
module exists because the Azure adapters carried the body from the start and the
local ones dropped it, which is not a decision anybody made — it is two files
written on different days, and it cost somebody a debugging session on exactly
the message above.
"""

from typing import Any

import httpx

#: How much of a provider's message to keep. A provider is free to echo the
#: request back inside its error, and these messages reach logs: an embedding
#: request carries an organization's documentation and a chat request carries
#: whatever somebody typed. Enough to diagnose, not enough to leak a corpus.
DETAIL_LIMIT = 200


def error_detail(response: httpx.Response, *, limit: int = DETAIL_LIMIT) -> str:
    """Summarise a failed response's body, ready to append to an error message.

    Deliberately tolerant, because "the shape of an error body" is the least
    standardised part of any API. It reads, in order: an ``error`` that is a
    string, an ``error`` object's ``message`` or ``code``, the same keys at the
    top level, and failing all of that the raw text — which is how an HTML page
    from a proxy that never reached the provider ends up identifying itself
    instead of arriving as a bare 502.

    Args:
        response: The response that failed.
        limit: Characters of provider text to keep.

    Returns:
        A parenthesised fragment such as ``" (InsufficientQuota)"``, or an empty
        string when the body says nothing worth repeating — so callers can
        append it unconditionally.
    """
    try:
        payload = response.json()
    except ValueError:
        return _fragment(response.text, limit)
    return _fragment(_message(payload) or "", limit)


def _message(payload: Any) -> str | None:
    """The most specific thing a decoded error body says, if it says anything."""
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return None

    error = payload.get("error", payload)
    if isinstance(error, str):
        return error
    if not isinstance(error, dict):
        return None

    for key in ("message", "code", "detail"):
        value = error.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _fragment(text: str, limit: int) -> str:
    """Collapse a provider's text onto one line and cap its length."""
    collapsed = " ".join(text.split())
    if not collapsed:
        return ""
    if len(collapsed) > limit:
        collapsed = collapsed[:limit].rstrip() + "…"
    return f" ({collapsed})"


__all__ = ["DETAIL_LIMIT", "error_detail"]
