"""A document source backed by GitHub's own MCP server.

GitHub is the one external system in this platform's reach that is worth
speaking a protocol to. Its API is somebody else's, its authentication is
somebody else's, and there is an official server that already exposes both — so
the alternative to this adapter is not "no protocol", it is a hand-rolled REST
client with its own pagination, its own rate-limit handling and its own bugs.

The endpoint is the ``repos`` toolset in its **read-only** form. Scoping at the
tool level rather than the server level is the least-privilege advice that
security guidance for MCP consumers keeps repeating, and here it is one path
segment: a synchronisation that cannot call ``delete_file`` cannot be talked
into calling it.

This module is where coupling to another team's tool names and output shapes is
allowed to live. That coupling is real and it will break one day; keeping it in
one adapter behind a port is what makes that a contained problem rather than a
platform-wide one.
"""

import base64
import binascii
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

from paimon.domain.errors import SourceContentError
from paimon.domain.ports import SourceContent, SourceReference
from paimon.infrastructure.sources.client import McpSession, McpToolClient, ToolContract

#: The one tool this source calls. It both lists a directory and returns a
#: file, depending on what the path points at.
GET_FILE_CONTENTS = "get_file_contents"

CONTRACTS = (ToolContract(GET_FILE_CONTENTS),)

#: Where the read-only repository toolset lives.
READONLY_REPOS_URL = "https://api.githubcopilot.com/mcp/x/repos/readonly"

MEDIA_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
}


@dataclass(frozen=True, slots=True)
class Repository:
    """One repository, and which part of it to index.

    Attributes:
        owner: Account or organization the repository belongs to.
        repo: Repository name.
        paths: Directories to walk. Empty means the repository root.
        ref: Branch, tag or commit. None takes the default branch, which means a
            synchronisation follows whatever that branch says today.
        max_depth: How far to descend. A documentation tree is shallow; a source
            tree is not, and walking one by accident is a long, expensive
            mistake to make silently.
        max_documents: Ceiling on what one synchronisation will offer.
    """

    owner: str
    repo: str
    paths: tuple[str, ...] = ()
    ref: str | None = None
    max_depth: int = 4
    max_documents: int = 200
    suffixes: tuple[str, ...] = (".md",)

    @property
    def slug(self) -> str:
        """Owner and name, as GitHub writes them."""
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True, slots=True)
class _Entry:
    """One item in a directory listing, reduced to what the walk needs."""

    path: str
    is_directory: bool


class GitHubDocumentSource:
    """Offers a repository's documentation as documents to ingest."""

    def __init__(self, client: McpToolClient, repository: Repository) -> None:
        """Initialise the source.

        Args:
            client: A verified client for GitHub's MCP server.
            repository: What to index, and how far.
        """
        self._client = client
        self._repository = repository

    @property
    def name(self) -> str:
        """Identifies this source in configuration, logs and errors."""
        return f"github:{self._repository.slug}"

    async def list(self) -> AsyncIterator[SourceReference]:
        """Walk the configured paths and yield every document found.

        One session for the whole walk rather than one per directory: opening a
        session costs a round trip and a verification, and a walk that paid for
        both at every level would spend most of its time doing neither.

        Yields:
            A reference per matching file.

        Raises:
            SourceUnavailableError: GitHub could not be reached.
            UntrustedSourceError: Its tool definitions no longer match.
        """
        repo = self._repository
        offered = 0
        async with self._client.session() as session:
            pending = [(path, 0) for path in (repo.paths or ("",))]
            while pending:
                path, depth = pending.pop()
                for entry in await self._listing(session, path):
                    if entry.is_directory:
                        if depth < repo.max_depth:
                            pending.append((entry.path, depth + 1))
                        continue
                    reference = self._reference(entry.path)
                    if reference is None:
                        continue
                    offered += 1
                    if offered > repo.max_documents:
                        msg = (
                            f"{self.name} offers more than {repo.max_documents} documents; "
                            "narrow 'paths' or raise the ceiling deliberately"
                        )
                        raise SourceContentError(msg)
                    yield reference

    async def fetch(self, reference: SourceReference) -> SourceContent:
        """Return one file's bytes.

        Args:
            reference: A reference this source yielded.

        Returns:
            The file's content, undecoded and uninterpreted.

        Raises:
            SourceUnavailableError: GitHub could not be reached.
            SourceContentError: The path no longer resolves, or is a directory.
        """
        path = reference.metadata["path"]
        async with self._client.session() as session:
            payload = _decode(await self._call(session, path))
        text = _file_content(payload)
        if text is None:
            msg = f"{self.name} returned no file content for '{path}'"
            raise SourceContentError(msg)
        return SourceContent(reference=reference, raw=text)

    async def _listing(self, session: McpSession, path: str) -> Sequence[_Entry]:
        """Return one directory's entries."""
        payload = _decode(await self._call(session, path))
        entries = _entries(payload)
        if entries is None:
            msg = f"{self.name} did not describe '{path or '/'}' as a directory"
            raise SourceContentError(msg)
        return entries

    async def _call(self, session: McpSession, path: str) -> str:
        """Ask for one path, whatever it turns out to be."""
        arguments: dict[str, Any] = {
            "owner": self._repository.owner,
            "repo": self._repository.repo,
            "path": path,
        }
        if self._repository.ref is not None:
            arguments["ref"] = self._repository.ref
        return await session.call(GET_FILE_CONTENTS, arguments)

    def _reference(self, path: str) -> SourceReference | None:
        """Describe a file, or decline it because of its extension."""
        repo = self._repository
        suffix = _suffix(path)
        if suffix not in repo.suffixes:
            return None
        ref = repo.ref or "HEAD"
        return SourceReference(
            # Slashes and all. A document id is an opaque string everywhere it is
            # used from here — it is an argument, never a URL path segment — and
            # one that reads like the thing it came from is worth more in a
            # citation than one that has been flattened to survive a route.
            document_id=f"github/{repo.owner}/{repo.repo}/{path}",
            source_uri=f"https://github.com/{repo.slug}/blob/{ref}/{path}",
            media_type=MEDIA_TYPES.get(suffix, "text/plain"),
            metadata={
                "source": self.name,
                "repository": repo.slug,
                "path": path,
                "ref": ref,
            },
        )


def _decode(text: str) -> Any:
    """Read the server's answer as JSON, or leave it as text.

    Tolerant on purpose. The protocol guarantees text, not a shape, and the
    shape belongs to a project that is not this one. Deciding here what arrived
    is more useful than a KeyError three frames deeper.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _suffix(path: str) -> str:
    """The lowercased extension of a path, including its dot."""
    name = path.rsplit("/", maxsplit=1)[-1]
    _, dot, extension = name.rpartition(".")
    return f"{dot}{extension}".lower() if dot else ""


def _entries(payload: Any) -> list[_Entry] | None:
    """Read a directory listing, or None if this is not one."""
    items = payload
    if isinstance(payload, dict):
        for key in ("entries", "items", "content", "files"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
        else:
            return None
    if not isinstance(items, list):
        return None

    entries: list[_Entry] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        path = item.get("path") or item.get("name")
        if not isinstance(path, str):
            return None
        kind = str(item.get("type") or item.get("kind") or "file").lower()
        entries.append(_Entry(path=path, is_directory=kind in {"dir", "directory", "tree"}))
    return entries


def _file_content(payload: Any) -> bytes | None:
    """Read a file's bytes out of whatever shape the answer arrived in."""
    if isinstance(payload, str):
        return payload.encode()
    if not isinstance(payload, dict):
        return None
    for key in ("text", "decoded", "raw"):
        value = payload.get(key)
        if isinstance(value, str):
            return value.encode()
    content = payload.get("content")
    if not isinstance(content, str):
        return None
    if str(payload.get("encoding", "")).lower() != "base64":
        return content.encode()
    return _from_base64(content)


def _from_base64(content: str) -> bytes | None:
    """Decode base64, or decline it. GitHub wraps file contents this way."""
    try:
        return base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        return None


__all__ = [
    "CONTRACTS",
    "GET_FILE_CONTENTS",
    "READONLY_REPOS_URL",
    "GitHubDocumentSource",
    "Repository",
]
