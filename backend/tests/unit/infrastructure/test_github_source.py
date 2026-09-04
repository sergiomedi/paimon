"""The GitHub source, against a stub of GitHub's MCP server.

The stub answers with the shapes the real server answers with — a JSON directory
listing, a base64 file — so the adapter's tolerance for them is exercised rather
than assumed. What it cannot check is that those shapes are still what GitHub
sends, which is the coupling this adapter exists to contain.
"""

import base64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from paimon.domain.errors import SourceContentError
from paimon.domain.ports import DocumentSource, SourceReference
from paimon.infrastructure.sources import GitHubDocumentSource, McpToolClient, Repository
from paimon.infrastructure.sources.client import Connect, ToolContract
from paimon.infrastructure.sources.github import GET_FILE_CONTENTS
from tests.contracts.document_source import REQUIRED, DocumentSourceContract

TREE: dict[str, Any] = {
    "docs": [
        {"path": "docs/alpha.md", "type": "file"},
        {"path": "docs/beta.md", "type": "file"},
        {"path": "docs/images", "type": "dir"},
        {"path": "docs/notes.txt", "type": "file"},
    ],
    "docs/images": [{"path": "docs/images/diagram.png", "type": "file"}],
}

FILES = {
    "docs/alpha.md": REQUIRED["alpha"],
    "docs/beta.md": REQUIRED["beta"],
    "docs/notes.txt": b"not markdown",
}


def stub_github() -> MCPServer:
    """A server answering the way GitHub's does."""
    server = MCPServer(name="stub-github")

    @server.tool(name=GET_FILE_CONTENTS, description="Get file or directory contents.")
    async def get_file_contents(owner: str, repo: str, path: str = "", ref: str = "") -> str:
        if path in TREE:
            return json.dumps(TREE[path])
        content = FILES.get(path)
        if content is None:
            msg = f"path '{path}' not found"
            raise ToolError(msg)
        # Base64, as GitHub's contents API returns it.
        return json.dumps(
            {"content": base64.b64encode(content).decode(), "encoding": "base64", "path": path}
        )

    return server


def connector(server: MCPServer) -> Connect:
    @asynccontextmanager
    async def connect() -> AsyncIterator[Client]:
        async with Client(server, raise_exceptions=True) as client:
            yield client

    return connect


def source_over(server: MCPServer, **overrides: Any) -> GitHubDocumentSource:
    """A GitHub source reading the stub."""
    client = McpToolClient(connector(server), [ToolContract(GET_FILE_CONTENTS)], server="stub")
    settings: dict[str, Any] = {"owner": "acme", "repo": "handbook", "paths": ("docs",)}
    settings.update(overrides)
    return GitHubDocumentSource(client, Repository(**settings))


class TestGitHubDocumentSource(DocumentSourceContract):
    """The adapter, run through the port's own contract."""

    @pytest.fixture
    def source(self) -> DocumentSource:
        return source_over(stub_github())


class TestWalkingARepository:
    async def test_only_the_configured_extensions_are_offered(self) -> None:
        source = source_over(stub_github())
        paths = {reference.metadata["path"] async for reference in source.list()}
        assert paths == {"docs/alpha.md", "docs/beta.md"}

    async def test_subdirectories_are_walked(self) -> None:
        source = source_over(stub_github(), suffixes=(".md", ".png"))
        paths = {reference.metadata["path"] async for reference in source.list()}
        assert "docs/images/diagram.png" in paths

    async def test_the_walk_stops_at_the_configured_depth(self) -> None:
        source = source_over(stub_github(), suffixes=(".md", ".png"), max_depth=0)
        paths = {reference.metadata["path"] async for reference in source.list()}
        assert "docs/images/diagram.png" not in paths

    async def test_a_repository_larger_than_the_ceiling_is_refused(self) -> None:
        # Loudly, and before the embeddings are paid for. A ceiling that is
        # silently exceeded is not a ceiling.
        source = source_over(stub_github(), max_documents=1)
        with pytest.raises(SourceContentError, match="more than 1 documents"):
            [reference async for reference in source.list()]

    async def test_a_reference_points_at_something_a_reader_can_open(self) -> None:
        source = source_over(stub_github(), ref="main")
        references = {r.metadata["path"]: r async for r in source.list()}
        alpha = references["docs/alpha.md"]
        assert alpha.source_uri == "https://github.com/acme/handbook/blob/main/docs/alpha.md"
        assert alpha.document_id == "github/acme/handbook/docs/alpha.md"
        assert alpha.media_type == "text/markdown"

    async def test_provenance_travels_with_the_document(self) -> None:
        # It ends up on the indexed document, which is what makes a citation
        # traceable back past this platform to where the text actually lives.
        source = source_over(stub_github())
        reference = await anext(source.list())
        assert reference.metadata["repository"] == "acme/handbook"
        assert reference.metadata["source"] == "github:acme/handbook"


class TestFetchingAFile:
    async def test_base64_content_is_decoded(self) -> None:
        source = source_over(stub_github())
        references = {r.metadata["path"]: r async for r in source.list()}
        content = await source.fetch(references["docs/alpha.md"])
        assert content.raw == REQUIRED["alpha"]

    async def test_a_path_that_no_longer_exists_is_refused(self) -> None:
        source = source_over(stub_github())
        stale = SourceReference(
            document_id="github/acme/handbook/docs/gone.md",
            source_uri="https://github.com/acme/handbook/blob/HEAD/docs/gone.md",
            media_type="text/markdown",
            metadata={"path": "docs/gone.md"},
        )
        with pytest.raises(SourceContentError):
            await source.fetch(stale)
