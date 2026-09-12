"""Every variable the deployment sets must be one this application consumes.

This test reads `infrastructure/modules/api.bicep` — a file outside the backend,
which is unusual and is the whole point. The template and the settings model are
two halves of one contract, they are edited weeks apart by different concerns,
and until now nothing compared them. The only thing that knows which variable
names are real is the settings model, so the check belongs where the model is.

It was written after the deployment it would have prevented. The template set
four observability variables that are entirely valid; the unknown-variable guard
walked only one level of nesting and reported them as typos; the container exited
1 two seconds into every start, eight times, five minutes apart, while an
environment billed by the hour. Nothing in the repository could have caught it:
the template compiled, the linter passed, the tests passed, the image built, the
deployment succeeded, and the first request came back 504 after four minutes.

The lesson is the one this phase keeps teaching. Ask the system that knows: the
model knows its own variable names, so ask it rather than reading the template
and believing it.
"""

import re
from pathlib import Path

import pytest

from paimon.config import unknown_environment_variables

#: Deployment templates that set configuration for a container running this
#: application. The migration and bootstrap jobs share api.bicep's own variables.
TEMPLATES = ("modules/api.bicep",)

#: A name and nothing else. The template writes them as `name: 'PAIMON_X'`.
VARIABLE = re.compile(r"name:\s*'(PAIMON_[A-Z0-9_]+)'")

REPOSITORY = Path(__file__).resolve().parents[4]
INFRASTRUCTURE = REPOSITORY / "infrastructure"


def declared() -> set[str]:
    """Every PAIMON_ variable the deployment templates set."""
    names: set[str] = set()
    for template in TEMPLATES:
        names |= set(VARIABLE.findall((INFRASTRUCTURE / template).read_text(encoding="utf-8")))
    return names


def test_the_templates_are_where_this_test_thinks_they_are() -> None:
    """Checked explicitly, because a moved file would make every assertion below
    pass against an empty set — a test that cannot fail, reporting success."""
    for template in TEMPLATES:
        assert (INFRASTRUCTURE / template).is_file(), f"{template} is not where it was"
    assert len(declared()) > 10, "suspiciously few variables found; has the syntax changed?"


@pytest.mark.parametrize("name", sorted(declared()))
def test_every_deployed_variable_is_one_this_application_consumes(name: str) -> None:
    """A variable the settings model does not recognise stops the process.

    Deliberately so: a typo in a deployment should refuse to start rather than
    silently run on a default. The cost of that decision is exactly
    this test — the deployment has to be checked against the model *before* the
    deployment, because afterwards the feedback is an exit code.
    """
    assert unknown_environment_variables({name: "x"}) == frozenset(), (
        f"{name} is set by the deployment and matches no setting. Either the "
        f"template's name is wrong, or the settings model no longer has that field."
    )
