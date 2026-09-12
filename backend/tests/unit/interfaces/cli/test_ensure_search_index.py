"""The search index command: what it refuses, and what it reports.

Driven against fake stores rather than a service. What is pinned is the one
decision the command makes — whether this deployment's store has an index at all
— because getting that wrong in either direction is silent: a store without one
would fail with an attribute error, and a store with one would be skipped.
"""

import pytest

from paimon.domain.ports import IndexDescriptor, ManagedIndex
from paimon.interfaces.cli import ensure_search_index


class Unmanaged:
    """pgvector: its index is a table the migration owns."""

    descriptor = IndexDescriptor(name="chunks", embedding_model_id="local", dimensions=1024)


class Managed(Unmanaged):
    """A search service: the index is a schema it holds."""

    def __init__(self) -> None:
        self.created = 0

    async def ensure_index(self) -> None:
        self.created += 1


class Resources:
    def __init__(self, store: object) -> None:
        self.vector_store = store

    async def __aenter__(self) -> "Resources":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class Settings:
    observability = object()

    class retrieval:  # noqa: N801  mirrors the settings attribute name
        store = "azure_search"


def arrange(monkeypatch: pytest.MonkeyPatch, store: object) -> None:
    monkeypatch.setattr(ensure_search_index, "get_settings", Settings)
    monkeypatch.setattr(ensure_search_index, "configure_logging", lambda _settings: None)
    monkeypatch.setattr(ensure_search_index, "build_resources", lambda _settings: Resources(store))


def test_the_capability_is_a_type_not_a_flag() -> None:
    """Which is the whole point of expressing it as a protocol."""
    assert isinstance(Managed(), ManagedIndex)
    assert not isinstance(Unmanaged(), ManagedIndex)


@pytest.mark.asyncio
async def test_creates_the_index_when_the_store_has_one(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Managed()
    arrange(monkeypatch, store)

    assert await ensure_search_index.run() == 0
    assert store.created == 1


@pytest.mark.asyncio
async def test_refuses_a_store_with_no_index_to_create(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reported as a usage error, not swallowed: somebody who ran this expected an
    index, and silence would let them believe they had one."""
    arrange(monkeypatch, Unmanaged())

    assert await ensure_search_index.run() == ensure_search_index.UNSUPPORTED


@pytest.mark.asyncio
async def test_running_it_twice_is_the_normal_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """The definition lives in the adapter, so a repeat brings an existing index up
    to it. Nobody should have to know whether it already exists."""
    store = Managed()
    arrange(monkeypatch, store)

    await ensure_search_index.run()
    await ensure_search_index.run()
    assert store.created == 2
