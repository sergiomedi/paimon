#!/usr/bin/env python3
"""Where the provider refused the content, and what that does to a comparison.

    scripts/azure/filtered.py evaluation/reports/azure-agents-v1-*.json

Azure OpenAI's content filter rejects some prompts outright with a 400. In the
agent window that happened on tasks whose corpus documents carry an injected
instruction: the request never reached the model, so there is no agent
behaviour to score — it is neither a pass nor a failure of the agent.

Two things follow, and this script exists for both.

**It is not only an injection-category problem.** The injected document can be
retrieved for any question, and the three systems search differently: the
investigator issues its own queries, the other two retrieve once. So the filter
can land on different tasks for different systems, and it is counted here by
system *and* category rather than assumed to sit where the dataset put it.

**It must not read as a difference between systems.** If one system is filtered
on a task and another is not, a paired comparison over that task is comparing an
agent against a platform refusal. So the paired difference is reported twice:
over every task, and over only the tasks where **no** system was filtered.
"""

import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

#: What the provider says when it refuses. Matched on the message rather than on
#: the status code: a 400 can also mean a malformed request, which is ours to
#: fix, while this one is the platform declining to process the content.
REFUSAL_MARKERS = ("content management policy", "content_filter", "responsible ai")


#: What the agent path reports when a node raises. It carries no cause, which
#: is the defect this script had to work around: the single-pass system reports
#: "azure openai chat returned 400 (...content management policy...)" for the
#: same task and the same trial, and the agent reports this.
UNATTRIBUTED = "the run failed"


def refused(failure: str) -> bool:
    """Whether this failure was the provider refusing the content, provably."""
    lowered = failure.lower()
    return any(marker in lowered for marker in REFUSAL_MARKERS)


def unattributed(failure: str) -> bool:
    """Whether the report records a failure without recording its cause."""
    return failure.strip().lower().startswith(UNATTRIBUTED)


#: A benchmark progress line, printed **after** a task finishes:
#: ``incident-triage x3 25/30  a028``. That ordering is what makes the
#: correlation below sound — the requests a task made appear above its line.
PROGRESS = re.compile(r"^(?P<system>[\w.-]+) x\d+ \d+/\d+\s+(?P<task>\S+)\s*$")

#: A refused request, as httpx logs it.
BAD_REQUEST = re.compile(r'^(?P<when>\S+).*POST (?P<url>\S+).*"HTTP/1\.1 400 Bad Request"')

#: Any logged request, used only to work out when each task's block began and
#: ended. Attribution by position is sound only while one task runs at a time.
ANY_REQUEST = re.compile(r'^(?P<when>\S+).*HTTP Request: \w+ (?P<url>\S+)')


class InterleavedTasks(Exception):
    """Two tasks' requests overlap in time, so position proves nothing.

    Raised rather than worked around. The whole argument for reading a task id
    off the next progress line is that the requests above it belong to that
    task, and that holds only while tasks run one at a time. If the benchmark
    ever runs them concurrently the blocks still *look* well formed — the
    progress lines are still in order — and every attribution is quietly wrong.
    A wrong attribution here excludes a task from a comparison on evidence that
    belongs to a different task, which is worse than excluding nothing.
    """


def evidence_from_log(path: Path) -> dict[tuple[str, str], list[tuple[int, str, str]]]:
    """Which 400s the log shows, and which task each one belongs to.

    The report does not record why an agent run failed — it says only "the run
    failed" — so excluding such a task from a comparison on the assumption that
    the provider refused it would be excluding it on a guess. This recovers the
    proof instead, or fails to, in which case the failure stays counted against
    the system.

    The correlation is positional and the log's own ordering is what licenses
    it: a progress line is printed when a task finishes, so every request that
    task made appears above its line and below the previous one. A 400 is
    therefore attributed to the next progress line for the same system.

    Returns:
        Per (system, task), the 400s found: log line number, timestamp, URL.
    """
    found: dict[tuple[str, str], list[tuple[int, str, str]]] = {}
    pending: list[tuple[int, str, str]] = []
    span: list[str] = []
    spans: list[tuple[str, str, str]] = []
    stripped = re.compile(r"\x1b\[[0-9;]*m")
    for number, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        line = stripped.sub("", raw)
        request = ANY_REQUEST.match(line)
        if request:
            span.append(request.group("when"))
        bad = BAD_REQUEST.match(line)
        if bad:
            pending.append((number, bad.group("when"), bad.group("url")))
            continue
        progress = PROGRESS.match(line)
        if not progress:
            continue
        key = (progress.group("system"), progress.group("task"))
        if span:
            spans.append((f"{key[0]}/{key[1]}", min(span), max(span)))
        if pending:
            found.setdefault(key, []).extend(pending)
        pending = []
        span = []

    _refuse_if_interleaved(spans)
    return found


def _refuse_if_interleaved(spans: list[tuple[str, str, str]]) -> None:
    """Check each task's requests finished before the next task's began.

    Timestamps, not positions: if two tasks ran at once their lines interleave
    in the file but their blocks still appear in progress order, so only the
    clock shows it. Overlapping blocks mean a request in one task's block may
    have been issued by another, and no attribution from this log is safe.

    Raises:
        InterleavedTasks: If any block starts before the previous one ended.
    """
    overlaps = [
        (spans[index - 1], spans[index])
        for index in range(1, len(spans))
        if spans[index][1] < spans[index - 1][2]
    ]
    if not overlaps:
        return
    lines = [
        f"    {before[0]} ran {before[1]}..{before[2]}, "
        f"{after[0]} began {after[1]} — before it finished"
        for before, after in overlaps[:5]
    ]
    joined = "\n".join(lines)
    msg = (
        f"{len(overlaps)} pair(s) of tasks overlap in time, so a request cannot be "
        f"attributed to a task by its position in the log:\n{joined}\n"
        "    Attribution refused. Every unattributed failure counts as a system failure."
    )
    raise InterleavedTasks(msg)


def read(path: Path) -> dict[str, object]:
    """One report, as written by the agent benchmark."""
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str]) -> int:
    """Report the filter's footprint and its effect on the paired difference."""
    log: Path | None = None
    if "--log" in argv:
        index = argv.index("--log")
        log = Path(argv[index + 1])
        argv = argv[:index] + argv[index + 2 :]
    evidence: dict[tuple[str, str], list[tuple[int, str, str]]] = {}
    if log and log.is_file():
        try:
            evidence = evidence_from_log(log)
        except InterleavedTasks as refusal:
            # Loudly, and with nothing excluded. A quiet fallback here would
            # look exactly like a log that simply had no 400s in it.
            print("REFUSING TO ATTRIBUTE FAILURES FROM THE LOG")
            print(f"  {refusal}\n")

    paths = [Path(a) for a in argv if Path(a).is_file()]
    if not paths:
        print(__doc__)
        return 2

    reports = {}
    for path in paths:
        raw = read(path)
        reports[str(raw.get("system", path.stem))] = raw

    # Per system: which tasks were refused, how often, and in which category.
    by_system: dict[str, Counter[str]] = {}
    refused_tasks: dict[str, set[str]] = defaultdict(set)
    categories: dict[str, str] = {}
    other_failures: dict[str, Counter[str]] = {}
    blind: dict[str, set[str]] = defaultdict(set)

    for system, raw in reports.items():
        cats: Counter[str] = Counter()
        others: Counter[str] = Counter()
        for task in raw.get("tasks", ()):
            task_id = str(task["task_id"])
            categories[task_id] = str(task.get("category", ""))
            for attempt in task.get("attempts", ()):
                failure = str(attempt.get("failed") or "")
                if not failure:
                    continue
                if refused(failure):
                    cats[categories[task_id]] += 1
                    refused_tasks[system].add(task_id)
                elif unattributed(failure):
                    # The report does not say why. Excluding the task on the
                    # assumption that the provider refused it would be
                    # excluding it on a guess, so the proof is looked up in the
                    # run log; without one this stays a failure of the system
                    # and is not excluded from anything.
                    blind[system].add(task_id)
                    others["unattributed"] += 1
                else:
                    others[failure.split(":", 1)[0]] += 1
        by_system[system] = cats
        other_failures[system] = others

    print("provider content-filter refusals, by system and category")
    every_category = sorted({c for cats in by_system.values() for c in cats})
    if not every_category:
        print("  none in these reports\n")
    else:
        width = max(len(s) for s in by_system)
        header = "  " + " " * width + "  " + "  ".join(f"{c:>16}" for c in every_category)
        print(header)
        for system, cats in sorted(by_system.items()):
            cells = "  ".join(f"{cats.get(c, 0):>16}" for c in every_category)
            print(f"  {system:<{width}}  {cells}")
        print()
        for system in sorted(refused_tasks):
            print(f"  {system}: tasks {sorted(refused_tasks[system])}")

    for system, others in sorted(other_failures.items()):
        if others:
            print(f"\n  {system} also had non-refusal failures: {dict(others)}")

    proven = {t for tasks in refused_tasks.values() for t in tasks}
    corroborated: set[str] = set()
    unexplained: dict[str, set[str]] = defaultdict(set)

    if blind:
        print("\nfailures the report gives no cause for, checked against the run log")
        if not evidence:
            print("  no --log given, so none of these can be excluded")
    for system in sorted(blind):
        for task_id in sorted(blind[system]):
            lines = evidence.get((system, task_id), [])
            if lines:
                corroborated.add(task_id)
                print(f"  {system} / {task_id}: {len(lines)} x HTTP 400 in the log")
                for number, when, url in lines:
                    print(f"      line {number}  {when}  POST {url.split('?')[0]}")
            else:
                unexplained[system].add(task_id)
                print(f"  {system} / {task_id}: NO 400 in the log -> counted as a system failure")

    tainted = sorted(proven | corroborated)
    print(f"\n  proven provider refusal (message in the report): {sorted(proven) or 'none'}")
    print(f"  corroborated by a 400 in the run log:           {sorted(corroborated) or 'none'}")
    for system in sorted(unexplained):
        print(f"  NOT excluded, {system} failed unexplained on:    {sorted(unexplained[system])}")
    print(f"  excluded from the clean comparison:             {tainted or 'none'}")
    if tainted:
        print("  (a paired comparison over a task where one side never reached the")
        print("   model compares an agent against a platform refusal)")

    # The paired difference, with and without those tasks.
    names = sorted(reports)
    if len(names) >= 2:
        print("\npaired differences in pass rate")
        order = [str(t["task_id"]) for t in next(iter(reports.values())).get("tasks", ())]
        for i, left in enumerate(names):
            for right in names[i + 1 :]:
                a = reports[left]["scores"]["pass_rate"]  # type: ignore[index]
                b = reports[right]["scores"]["pass_rate"]  # type: ignore[index]
                if len(a) != len(order) or len(b) != len(order):
                    print(f"  {left} vs {right}: task counts differ, skipped")
                    continue
                everywhere = [x - y for x, y in zip(a, b, strict=True)]
                clean = [
                    x - y
                    for task_id, x, y in zip(order, a, b, strict=True)
                    if task_id not in tainted
                ]
                print(f"  {left} - {right}")
                print(f"    over all {len(everywhere):>2} tasks       {statistics.fmean(everywhere):+.1%}")
                if clean and len(clean) != len(everywhere):
                    print(f"    over {len(clean):>2} unfiltered tasks  {statistics.fmean(clean):+.1%}")
                elif not tainted:
                    print("    (no filtered tasks, so the two are the same)")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
