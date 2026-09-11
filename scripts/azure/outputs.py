#!/usr/bin/env python3
"""Turn a deployment's outputs into shell assignments.

Reads the JSON object that ``az deployment sub show --query properties.outputs``
produces on standard input, and writes ``AZURE_SCREAMING_SNAKE=value`` lines.

**This is a file rather than a string inside deploy.sh, and that is the point.**
It lived there as an argument to ``python3 -c`` until an escaped quote inside an
f-string made it a syntax error — after ``bash -n`` had checked the script and
reported no problem, because a shell syntax check does not check the syntax of a
program embedded in a shell string. The deployment succeeded and the last line of
the script died. Code that no gate can see is code that has no gate.

``scripts/check.sh`` now compiles this file and runs it against a fixture.
"""

import json
import re
import sys


def render(outputs: dict[str, dict[str, object]]) -> str:
    """Render deployment outputs as shell assignments, sorted by name.

    Args:
        outputs: The ``properties.outputs`` object, mapping an output name to an
            object with a ``value``.

    Returns:
        The file's contents, newline-terminated.
    """
    lines = ["# Written by scripts/azure/deploy.sh. Regenerated on every deployment."]
    for name in sorted(outputs):
        # containerRegistryLoginServer -> CONTAINER_REGISTRY_LOGIN_SERVER
        shouted = re.sub(r"(?<!^)(?=[A-Z])", "_", name).upper()
        lines.append("AZURE_" + shouted + "=" + str(outputs[name]["value"]))
    return "\n".join(lines) + "\n"


def main() -> int:
    """Read outputs from stdin and write assignments to stdout."""
    try:
        outputs = json.load(sys.stdin)
    except json.JSONDecodeError as error:
        sys.stderr.write(f"deployment outputs are not JSON: {error}\n")
        return 1
    if not isinstance(outputs, dict):
        sys.stderr.write("deployment outputs are not an object\n")
        return 1
    sys.stdout.write(render(outputs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
