"""Stand-in for a domain package that imports an adapter, which is forbidden."""

from violating import infrastructure  # noqa: F401  the violation under test
