"""Tests for the architecture enforcement itself.

The dependency rule of ADR-0002 is only as good as the checker that enforces it.
These tests assert both directions: that the real contracts hold, and that the
checker actually fails when a contract is broken. A guard that has never been
observed to fail is not a guard.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def lint_imports() -> str:
    executable = shutil.which("lint-imports")
    if executable is None:  # pragma: no cover - only hit on a broken environment
        pytest.fail("lint-imports is not installed; run 'uv sync --all-groups'")
    return executable


def test_real_contracts_hold(lint_imports: str) -> None:
    result = subprocess.run(  # noqa: S603  trusted executable resolved from PATH
        [lint_imports],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_checker_detects_a_violation(lint_imports: str) -> None:
    """A package that breaks the rule must make the checker exit non-zero."""
    result = subprocess.run(  # noqa: S603  trusted executable resolved from PATH
        [lint_imports, "--config", ".importlinter"],
        cwd=FIXTURES,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, "the checker passed a package that violates the rule"
    assert "The domain is independent" in result.stdout
