"""Flatten the error ARM buries inside a failed deployment operation.

Reads the `properties.statusMessage` objects of failed operations on standard
input, as a JSON list, and writes one indented line per error.

This exists because of a deployment that failed like this:

    InvalidTemplateDeployment — the template deployment 'ai' is not valid
    according to the validation procedure. The following resource provider(s) -
    'Microsoft.CognitiveServices/accounts' reported preflight validation errors.
    See inner errors for details.

"See inner errors for details" is the whole problem. The detail is two or three
levels down an `error.details[]` chain that the CLI prints as one line of JSON
when it prints it at all, and the outer message names neither the resource nor
the reason. Finding it took two days and an archaeological dig through the
subscription's deployment history after the environment had been destroyed.

Nothing here is clever. It walks the chain and prints what is at the bottom,
which is the sentence somebody can act on.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Iterator


def errors(node: Any, depth: int = 0) -> Iterator[tuple[int, str, str]]:
    """Yield (depth, code, message) for an error and everything nested in it.

    Depth is carried so the caller can indent: the shape of the chain is itself
    informative — a preflight error under a nested deployment under a
    subscription deployment is three different scopes reporting the same
    failure, and seeing that is how you know which template to look in.
    """
    if not isinstance(node, dict):
        return

    code = str(node.get("code") or "").strip()
    message = str(node.get("message") or "").strip()
    if code or message:
        yield depth, code, message

    # ARM is inconsistent about where it nests: `details` is the documented
    # chain, `error` appears when a status message wraps another status message,
    # and `innererror` shows up from some resource providers. All three are
    # followed rather than guessed between.
    for key in ("error", "innererror"):
        yield from errors(node.get(key), depth + 1)
    for child in node.get("details") or []:
        yield from errors(child, depth + 1)


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        return 0

    try:
        messages = json.loads(raw)
    except json.JSONDecodeError:
        # Not the shape expected. Print it whole rather than print nothing: the
        # error nobody recognises is exactly the one worth seeing verbatim, and
        # a parser that swallows it is worse than no parser at all.
        print(raw)
        return 0

    if isinstance(messages, dict):
        messages = [messages]

    seen: set[tuple[str, str]] = set()
    for status_message in messages:
        for depth, code, message in errors(status_message):
            # ARM repeats the same error at several levels of the chain often
            # enough that the duplicates outnumber the detail.
            if (code, message) in seen:
                continue
            seen.add((code, message))

            indent = "  " + "  " * depth
            if code and message:
                print(f"{indent}{code}: {message}")
            else:
                print(f"{indent}{code or message}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
