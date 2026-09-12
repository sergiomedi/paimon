#!/usr/bin/env python3
"""The two pieces of the measured run that are not a shell's work.

``body`` turns a file into an ingestion request, and ``answer`` turns the API's
answer into the one line worth recording. Both exist as a file rather than as a
``python3 -c`` argument inside measure.sh for the reason written on outputs.py:
code embedded in a shell string is invisible to every gate this repository has,
and the one time it mattered a syntax error in such a string survived ``bash -n``
and killed a deployment script on its last line.

Usage::

    python3 scripts/azure/measurement.py body --document-id oncall-handbook \\
        --source-uri file://oncall-handbook.md evaluation/corpus/sample/oncall-handbook.md

    curl ... | python3 scripts/azure/measurement.py answer
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

#: How much of an answer to quote in the record. Enough to see whether it
#: answered the question; not so much that the file becomes a transcript.
QUOTE = 300


def ingest_body(path: Path, document_id: str, source_uri: str) -> str:
    """Render the ingestion request for a file.

    Args:
        path: The document to send.
        document_id: The id to store it under. The API treats ingestion as
            idempotent by this id, which is why the measured run can be repeated
            without accumulating documents.
        source_uri: Where the document came from, recorded on every citation.

    Returns:
        The request body, as JSON.
    """
    return json.dumps(
        {
            "source_uri": source_uri,
            "content": path.read_text(encoding="utf-8"),
            "media_type": "text/markdown",
            "metadata": {"origin": "measured-run"},
        }
    )


def describe_ingestion(response: dict[str, Any]) -> str:
    """Say what ingesting that document actually did.

    Two outcomes, and the interesting one is the cheap one. Ingestion is
    idempotent by document id: sending the same content twice reports
    ``unchanged`` and does no work at all — no parse, no embeddings, no write.
    The report used to assert that everything had been embedded and indexed
    regardless, which on a second run was simply untrue, and it described the
    platform's most defensible property as if it had not happened.
    """
    if "document_id" not in response:
        return f"no ingestion result in the response: {json.dumps(response)[:QUOTE]}"

    chunks = response.get("chunks_indexed", 0)
    if response.get("unchanged"):
        return (
            f"**unchanged**, {chunks} chunks. The content hash matched, so nothing was parsed, "
            "embedded or written: ingestion is idempotent by document id, and this is what that "
            "costs on a repeat — a hash comparison. Compare it with the first ingestion of the "
            "same document in an earlier run."
        )
    return (
        f"{chunks} chunks indexed. Parsed, chunked, embedded through Azure OpenAI and written to "
        "PostgreSQL over the private endpoint, as one request."
    )


def describe(response: dict[str, Any]) -> str:
    """Summarise an answer response in the terms the record cares about.

    Grounding first, because a confident answer with no citations is the failure
    this platform exists to make visible rather than the success it looks like.
    """
    if "answer" not in response:
        return f"no answer in the response: {json.dumps(response)[:QUOTE]}"

    citations = response.get("citations") or []
    usage = response.get("usage") or {}
    parts = [
        "grounded" if response.get("grounded") else "NOT grounded",
        f"{len(citations)} citations",
        f"{response.get('retrieved', '?')} retrieved",
        f"strategy {response.get('strategy', '?')}",
    ]
    if usage:
        parts.append(
            f"{usage.get('total_tokens', '?')} tokens "
            f"({usage.get('input_tokens', '?')} in, {usage.get('output_tokens', '?')} out)"
        )
        if usage.get("model_id"):
            parts.append(str(usage["model_id"]))
    if response.get("dropped_markers"):
        parts.append(f"dropped markers {response['dropped_markers']}")

    text = " ".join(str(response["answer"]).split())
    if len(text) > QUOTE:
        text = text[:QUOTE] + "…"
    return " · ".join(parts) + "\n\n> " + text


def main(argv: list[str] | None = None) -> int:
    """Run whichever half was asked for."""
    parser = argparse.ArgumentParser(prog="measurement")
    commands = parser.add_subparsers(dest="command", required=True)

    body = commands.add_parser("body", help="Render an ingestion request for a file.")
    body.add_argument("path", type=Path)
    body.add_argument("--document-id", required=True)
    body.add_argument("--source-uri", required=True)

    commands.add_parser("answer", help="Summarise an answer response read from stdin.")
    commands.add_parser("ingestion", help="Summarise an ingestion response read from stdin.")

    arguments = parser.parse_args(argv)

    if arguments.command == "body":
        sys.stdout.write(
            ingest_body(arguments.path, arguments.document_id, arguments.source_uri)
        )
        return 0

    raw = sys.stdin.read()
    try:
        response = json.loads(raw)
    except json.JSONDecodeError:
        # Printed rather than swallowed: an HTML error page or an empty body is
        # itself the measurement, and a run that hides it records a blank where
        # the interesting thing happened.
        sys.stdout.write(f"the response was not JSON: {' '.join(raw.split())[:QUOTE]}\n")
        return 1
    if not isinstance(response, dict):
        sys.stdout.write(f"the response was not an object: {raw[:QUOTE]}\n")
        return 1
    summarise = describe_ingestion if arguments.command == "ingestion" else describe
    sys.stdout.write(summarise(response) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
