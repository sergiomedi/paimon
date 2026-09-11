#!/usr/bin/env python3
"""Check that the collector's configuration is valid YAML and internally consistent.

Bicep loads ``infrastructure/collector.yaml`` with ``loadTextContent()``, which
verifies that the file exists and nothing else. A template that compiles cleanly
around a malformed configuration is a deployment that succeeds and a collector
that crash-loops — with the reason visible only in that container's own logs.

This is a program in a file rather than a string inside a shell script, for the
reason recorded in ``scripts/azure/outputs.py``: ``bash -n`` checks the shell and
says nothing at all about a program it passes to an interpreter as an argument.

Not a schema check — the collector owns its own schema, and duplicating it here
would be a second definition to keep in step. What this catches is the class of
mistake that produces a plausible-looking file: a pipeline naming a component
that was never defined, or an extension switched on that does not exist.

Usage:
    python3 scripts/azure/collector_config.py [path]
"""

from __future__ import annotations

import pathlib
import sys
from typing import Any

# The two values modules/observability.bicep substitutes at deployment time. They
# are replaced with something well-formed here so that the file parses as the YAML
# it will be, rather than as the template it is on disk.
PLACEHOLDERS = {
    "__CLIENT_ID__": "00000000-0000-0000-0000-000000000000",
    "__CONNECTION_STRING__": "InstrumentationKey=00000000-0000-0000-0000-000000000000",
}

DEFAULT_PATH = "infrastructure/collector.yaml"

PIPELINE_SECTIONS = ("receivers", "processors", "exporters")


def check(text: str) -> str:
    """Validate one configuration, returning a one-line summary.

    Args:
        text: Contents of the configuration file, placeholders included.

    Returns:
        A short description of what was checked.

    Raises:
        ValueError: If the configuration is inconsistent.
        ImportError: If PyYAML is not installed.
    """
    import yaml  # noqa: PLC0415  optional at import time, required at call time

    for placeholder, value in PLACEHOLDERS.items():
        text = text.replace(placeholder, value)

    config: dict[str, Any] = yaml.safe_load(text)

    for section in (*PIPELINE_SECTIONS, "extensions"):
        if not config.get(section):
            msg = f"'{section}' is missing or empty"
            raise ValueError(msg)

    pipelines: dict[str, Any] = config["service"]["pipelines"]
    for name, pipeline in pipelines.items():
        for section in PIPELINE_SECTIONS:
            for component in pipeline.get(section, []):
                if component not in config[section]:
                    msg = f"pipeline '{name}' uses {section[:-1]} '{component}', which is not defined"
                    raise ValueError(msg)

    for extension in config["service"].get("extensions", []):
        if extension not in config["extensions"]:
            msg = f"service enables extension '{extension}', which is not defined"
            raise ValueError(msg)

    return f"{len(pipelines)} pipelines, every component defined"


def main() -> int:
    """Check the file named on the command line, or the default one."""
    path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH)
    try:
        summary = check(path.read_text(encoding="utf-8"))
    except ImportError:
        # Not a failure. PyYAML is not a dependency of this repository and a
        # laptop may not have it; CI installs it, and CI is the authority.
        print(f"  PyYAML is not installed, skipping {path}")
        return 0
    except (ValueError, KeyError) as error:
        print(f"  {path}: {error}", file=sys.stderr)
        return 1
    print(f"  {path}: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
