"""Create the search index this deployment's configuration describes.

A deliberate command, run by a person, for the same reason the database bootstrap
is one (ADR-0042): defining an index is an administrator's act, and the workload
deliberately holds no privilege to redefine the shape of the index it writes to.
The identity that runs this is whoever is signed in — `az login` on a laptop —
and the deployment grants that person the data-plane role that allows it.

It is idempotent. The index definition lives in the adapter, so running this
again after a change to chunking, embedding dimensions or field mapping brings an
existing index up to the current definition rather than failing on it.

This exists as a command rather than as four lines of Python in a guide because
those four lines were in a guide, and a sequence somebody types correctly under
pressure is not a procedure. It is also the only thing in this phase that puts a
real request through the Azure AI Search adapter — which has been verified for
five phases against an in-process stand-in written by the author of the adapter.

Usage::

    uv run python -m paimon.interfaces.cli.ensure_search_index
"""

import asyncio
import sys

from paimon.config import get_settings
from paimon.domain.ports import ManagedIndex
from paimon.interfaces.api.dependencies import build_resources
from paimon.observability import configure_logging, get_logger

logger = get_logger(__name__)

UNSUPPORTED = 2

NOT_MANAGED = (
    "this deployment's vector store has no index to create. pgvector's index is a "
    "table the migration owns, so there is nothing for this command to do — set "
    "PAIMON_RETRIEVAL__STORE=azure_search to point it at a store that has one."
)


async def run() -> int:
    """Create or update the index, and say which one.

    Returns:
        A process exit code.
    """
    settings = get_settings()
    configure_logging(settings.observability)

    async with build_resources(settings) as resources:
        store = resources.vector_store
        if not isinstance(store, ManagedIndex):
            logger.error("index_not_managed", reason=NOT_MANAGED, store=settings.retrieval.store)
            return UNSUPPORTED

        descriptor = store.descriptor
        await store.ensure_index()
        logger.info(
            "index_ready",
            store=settings.retrieval.store,
            index=descriptor.name,
            model=descriptor.embedding_model_id,
            dimensions=descriptor.dimensions,
        )
    return 0


def main() -> int:
    """Entry point."""
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
