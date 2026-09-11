#!/usr/bin/env python3
"""Can this subscription create the model deployments the template asks for?

``az deployment sub validate`` does **not** answer this. It checks the template,
the providers, the SKUs of ordinary resources and regional eligibility, and it
returns success for a region with no model quota whatsoever — which is how a
region probe came back with ten green rows for a subscription that could not
deploy a chat model in any of them.

So this asks the question validate does not: for each deployment in the template,
is there a quota row for that **model × deployment type × region**, with a limit
above zero? Those three dimensions are independent, and a subscription routinely
has plenty of one combination and nothing at all in the neighbouring one.

The names do not match, which is the trap this exists to absorb. The model
catalogue calls it ``gpt-4.1-mini``; the quota row is
``OpenAI.GlobalStandard.gpt4.1-mini``, with no hyphen after ``gpt``. Searching the
quota list for the model's own name finds nothing and reads exactly like an
absence of quota. It cost two days here.

Usage::

    bicep build-params infrastructure/main.bicepparam --stdout > params.json
    python3 scripts/azure/model_quota.py --parameters params.json --list
    az cognitiveservices usage list --location swedencentral -o json |
        python3 scripts/azure/model_quota.py --parameters params.json --usage -
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, NamedTuple

#: The deployments the template creates, as (parameter holding the deployment
#: type, parameter holding the model). Kept here rather than discovered, because
#: a template that gains a third deployment should fail this check loudly by
#: being absent from it rather than quietly by not being looked for.
DEPLOYMENTS = (("chatSku", "chatModel"), ("embeddingSku", "embeddingModel"))


class Wanted(NamedTuple):
    """One deployment the template asks for."""

    sku: str
    model: str

    def __str__(self) -> str:
        """Render as the quota row would name it, near enough to read."""
        return f"OpenAI.{self.sku}.{self.model}"


def normalise(name: str) -> str:
    """Reduce a model name to something both spellings agree on.

    ``gpt-4.1-mini`` and ``gpt4.1-mini`` are the same model under two names — the
    catalogue's and the quota list's. Dropping the separators makes them equal
    without pretending to know which other pairs differ.

    Args:
        name: A model name from either source.

    Returns:
        The name lowercased with ``-``, ``.`` and ``_`` removed.
    """
    return name.lower().replace("-", "").replace(".", "").replace("_", "")


def resolve(build_params: dict[str, Any]) -> dict[str, str]:
    """Resolve the template's parameters the way a deployment would.

    ``bicep build-params`` emits the parameter file and the compiled template
    separately, and a parameter absent from the first takes its default from the
    second. Reading only the parameter file reports nothing for every value that
    was left at its default — which here is all four of them.

    Args:
        build_params: The object ``bicep build-params --stdout`` prints.

    Returns:
        Parameter name to value, defaults included.
    """
    template = json.loads(build_params["templateJson"])
    resolved = {
        name: definition["defaultValue"]
        for name, definition in template.get("parameters", {}).items()
        if "defaultValue" in definition
    }
    supplied = json.loads(build_params["parametersJson"]).get("parameters", {})
    resolved.update({name: entry["value"] for name, entry in supplied.items() if "value" in entry})
    return resolved


def wanted(parameters: dict[str, Any]) -> list[Wanted]:
    """The deployments the template asks for, as quota rows would name them."""
    return [
        Wanted(sku=str(parameters[sku_parameter]), model=str(parameters[model_parameter]))
        for sku_parameter, model_parameter in DEPLOYMENTS
        if sku_parameter in parameters and model_parameter in parameters
    ]


def available(usages: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
    """Index a region's quota list by deployment type and normalised model."""
    found: dict[tuple[str, str], int] = {}
    for usage in usages:
        name = usage.get("name", {}).get("value", "")
        # maxsplit, because the model name contains dots of its own:
        # `OpenAI.GlobalStandard.gpt4.1-mini` is three fields, not four, and
        # splitting on every dot silently drops exactly the rows that matter.
        parts = name.split(".", 2)
        if len(parts) != 3 or parts[0] != "OpenAI":
            continue
        _, sku, model = parts
        found[sku.lower(), normalise(model)] = int(usage.get("limit") or 0)
    return found


def missing(usages: list[dict[str, Any]], deployments: list[Wanted]) -> list[str]:
    """Report the deployments this region cannot host, with why.

    Args:
        usages: The region's quota list, as ``az cognitiveservices usage list``
            returns it.
        deployments: What the template asks for.

    Returns:
        One line per problem, empty when every deployment has quota.
    """
    found = available(usages)
    problems: list[str] = []
    for deployment in deployments:
        limit = found.get((deployment.sku.lower(), normalise(deployment.model)))
        if limit is None:
            problems.append(f"{deployment}: no quota row at all")
        elif limit <= 0:
            problems.append(f"{deployment}: quota row present, limit 0")
    return problems


class Unreadable(Exception):
    """The input was not the JSON this expected."""


def read(path: str) -> Any:
    """Read JSON from a path, or from standard input when the path is ``-``.

    Raises:
        Unreadable: If the input is missing, empty or not JSON. A quota list that
            could not be fetched arrives here as an empty stream, and a traceback
            in the middle of a region table is a worse answer than a sentence.
    """
    try:
        text = sys.stdin.read() if path == "-" else pathlib.Path(path).read_text(encoding="utf-8")
    except OSError as error:
        msg = f"could not read {path}: {error}"
        raise Unreadable(msg) from error
    if not text.strip():
        msg = f"{path} was empty"
        raise Unreadable(msg)
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        msg = f"{path} is not JSON: {error}"
        raise Unreadable(msg) from error


def main(argv: list[str] | None = None) -> int:
    """Check one region's quota, or list what the template asks for."""
    parser = argparse.ArgumentParser(
        prog="model-quota",
        description="Whether a region has quota for the model deployments in the template.",
    )
    parser.add_argument(
        "--parameters",
        required=True,
        help="Output of `bicep build-params --stdout`, or - for standard input.",
    )
    parser.add_argument(
        "--usage",
        help="Output of `az cognitiveservices usage list -o json`, or - for standard input.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print the deployments the template asks for, and stop.",
    )
    arguments = parser.parse_args(argv)

    try:
        deployments = wanted(resolve(read(arguments.parameters)))
    except (Unreadable, KeyError, ValueError) as error:
        print(f"could not read the template parameters: {error}")
        return 1
    if not deployments:
        print("  the template declares no model deployments", file=sys.stderr)
        return 1

    if arguments.list:
        for deployment in deployments:
            print(deployment)
        return 0

    if not arguments.usage:
        parser.error("one of --usage or --list is required")

    try:
        usages = read(arguments.usage)
    except Unreadable as error:
        print(f"could not read the quota list: {error}")
        return 1

    problems = missing(usages, deployments)
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
