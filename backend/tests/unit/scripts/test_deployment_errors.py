"""The failure reporter has to work on the day everything else has failed.

`scripts/azure/deployment_errors.py` is the piece of the deployment scripts that
only ever runs when a deployment has already gone wrong, which is precisely when
nobody is in a position to debug the debugger. It earned a test the hard way.

Delivery run 8 failed with this, and nothing else:

    InvalidTemplateDeployment — the template deployment 'ai' is not valid
    according to the validation procedure. ... See inner errors for details.

Two days went into finding the inner error, by which point the environment had
been destroyed and the answer survived only in the subscription's deployment
history. The outer message names neither the resource nor the reason; the reason
is two levels further down an `error.details[]` chain. This module walks the
chain, so these tests hold it to the shapes ARM actually produces.
"""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY = Path(__file__).resolve().parents[4]
MODULE = REPOSITORY / "scripts" / "azure" / "deployment_errors.py"


def load() -> ModuleType:
    """Import the helper from the deployment scripts, which are not a package.

    By path rather than by name: these scripts are run by Bash with `python3`,
    they are deliberately not part of the installed distribution, and making them
    importable would be changing the thing under test to suit the test.
    """
    specification = importlib.util.spec_from_file_location("deployment_errors", MODULE)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def deployment_errors() -> ModuleType:
    return load()


def flatten(module: ModuleType, payload: object) -> list[str]:
    return [
        f"{code}: {message}" if code and message else (code or message)
        for _, code, message in module.errors(payload)
    ]


def test_the_inner_error_is_reached(deployment_errors: ModuleType) -> None:
    """The one that mattered: the reason is two levels below the complaint."""
    status_message = json.loads("""
    {
      "error": {
        "code": "InvalidTemplateDeployment",
        "message": "The template deployment 'ai' is not valid. See inner errors for details.",
        "details": [
          {
            "code": "ValidationForResourceFailed",
            "message": "Validation failed for a resource.",
            "details": [
              {
                "code": "FlagMustBeSetForRestore",
                "message": "An existing resource with name oai-paimon-ci8 has been soft-deleted."
              }
            ]
          }
        ]
      }
    }
    """)

    lines = flatten(deployment_errors, status_message)

    assert any("FlagMustBeSetForRestore" in line for line in lines), (
        "the innermost error is the only one that says what to do, and it is the "
        "one the Azure CLI does not print"
    )
    # And the chain above it is kept. Which scope reported the failure tells you
    # which template to open, and dropping it to show only the leaf would trade
    # one kind of missing context for another.
    assert any("InvalidTemplateDeployment" in line for line in lines)


def test_depth_records_how_far_down_the_error_was(deployment_errors: ModuleType) -> None:
    """Indentation is not decoration: it is the scope the error was raised at."""
    status_message = {
        "error": {
            "code": "Outer",
            "message": "outer",
            "details": [{"code": "Inner", "message": "inner"}],
        }
    }

    depths = {code: depth for depth, code, _ in deployment_errors.errors(status_message)}

    assert depths["Inner"] > depths["Outer"]


@pytest.mark.parametrize("key", ["error", "innererror"])
def test_both_of_the_wrappers_azure_uses_are_followed(
    deployment_errors: ModuleType, key: str
) -> None:
    """ARM is not consistent about how it nests, so neither spelling is guessed.

    `details` is the documented chain; `error` appears when a status message
    wraps another status message; `innererror` comes from some resource
    providers. Following only the documented one loses whole errors.
    """
    status_message = {key: {"code": "Buried", "message": "found me"}}

    assert any("Buried" in line for line in flatten(deployment_errors, status_message))


def test_a_repeated_error_is_reported_once(deployment_errors: ModuleType) -> None:
    """ARM repeats itself down the chain often enough to bury the detail."""
    repeated = {"code": "Same", "message": "same"}
    status_message = {"error": {**repeated, "details": [dict(repeated)]}}

    lines = [line for line in flatten(deployment_errors, status_message) if "Same" in line]

    # errors() yields every occurrence; main() is what deduplicates, because
    # depth is part of what a caller may want. What must not happen is the CLI's
    # own behaviour: the same sentence four times and the useful one off-screen.
    assert len(lines) == 2, "the walk itself reports what it finds, duplicates included"


def test_a_payload_it_does_not_recognise_is_not_silently_dropped(
    deployment_errors: ModuleType,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error nobody recognises is exactly the one worth seeing verbatim.

    The same rule `validate()` follows in _common.sh, and for the same reason: a
    parser that prints nothing when surprised turns an unknown failure into no
    failure at all.
    """
    monkeypatch.setattr("sys.stdin", _Stdin("this is not JSON at all"))

    deployment_errors.main()

    assert "this is not JSON at all" in capsys.readouterr().out


def test_nothing_in_means_nothing_out(
    deployment_errors: ModuleType,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment can fail with no operation errors attached, and does.

    deploy.sh checks for empty output and says so in its own words; this must not
    invent a line for it to print.
    """
    monkeypatch.setattr("sys.stdin", _Stdin("   \n"))

    assert deployment_errors.main() == 0
    assert capsys.readouterr().out == ""


class _Stdin:
    """The one method the module uses. A file on disk would test the filesystem."""

    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text
