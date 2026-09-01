"""Smoke test proving the package is importable from the installed environment."""

import paimon


def test_package_exposes_a_version() -> None:
    assert paimon.__version__
