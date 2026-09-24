#!/usr/bin/env python3
"""The primary diagnostic: does the model take a second hop?

    scripts/azure/hops.py evaluation/reports/azure-agents-*.json

Reports, per agent report, the **distribution** of tool calls per run and how
many attempts cite two distinct documents — not the mean of either.

Why a distribution. Locally, qwen2.5:7b-instruct made exactly one tool call in
100 of 100 held-out runs, on both passage layouts. A mean of 1.00 and a mean of
1.09 look like nearly the same number and are not the same behaviour: the first
says the model never once tried a second step, the second says it sometimes
did. Only the histogram tells them apart, and the whole question this window
exists to answer lives in that difference.

Why two documents. Every multi-hop task needs evidence from two documents, and
one search can only reach both by accident. Attempts citing two distinct
documents is therefore the outcome the second hop would buy, measured directly
rather than inferred from a pass rate that also moves for other reasons.

The local baseline these are read against:

    investigator     tool calls {1: 50}   two documents 11 / 50
    investigator-v1  tool calls {1: 50}   two documents 10 / 50
"""

import json
import sys
from collections import Counter
from pathlib import Path

#: What the local held-out run did, per system, as `{calls: runs}` and the
#: count of attempts reaching two distinct documents out of fifty.
LOCAL = {
    "investigator": ({1: 50}, 11, 50),
    "investigator-v1": ({1: 50}, 10, 50),
}


def summarise(path: Path) -> None:
    """Print one report's hop behaviour."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    system = raw.get("system", "?")
    attempts = [a for t in raw.get("tasks", ()) for a in t.get("attempts", ()) if not a.get("failed")]
    if not attempts:
        print(f"  {path.name}: no completed attempts")
        return

    calls = Counter(a["trajectory"].get("tool_calls") or 0 for a in attempts)
    documents = Counter(len({c["document_id"] for c in a.get("citations", ())}) for a in attempts)
    total = len(attempts)
    two_plus = sum(n for k, n in documents.items() if k >= 2)
    multi_hop = sum(1 for k, n in calls.items() if k >= 2 for _ in range(n))

    print(f"\n  {system}  ({raw.get('dataset', '?')}, {total} attempts)")
    print(f"    tool calls per run   {dict(sorted(calls.items()))}")
    print(f"      more than one      {multi_hop} / {total}")
    print(f"    distinct documents   {dict(sorted(documents.items()))}")
    print(f"      two or more        {two_plus} / {total}")

    baseline = LOCAL.get(system)
    if baseline is not None:
        local_calls, local_two, local_total = baseline
        print(f"    local held-out was   {local_calls}, two documents {local_two} / {local_total}")
        if multi_hop:
            print("    -> the model took a second hop here and never did locally")
        else:
            print("    -> still exactly one hop, as locally")


def main(argv: list[str]) -> int:
    """Summarise every report named on the command line."""
    paths = [Path(a) for a in argv]
    if not paths:
        print(__doc__)
        return 2
    print("tool calls per run, and attempts reaching two documents")
    for path in paths:
        if path.is_file():
            summarise(path)
        else:
            print(f"  {path}: not a file")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
