"""Adapters implementing the DocumentSource port.

Named for the port rather than for the protocol underneath it. Today every
source here speaks MCP; a source that reads a mounted directory would belong in
this package too, and the day one does, nothing above it should have to move.
"""

from paimon.infrastructure.sources.client import (
    McpSession,
    McpToolClient,
    ToolContract,
    fingerprint,
    http_endpoint,
    verify_url,
)
from paimon.infrastructure.sources.github import (
    READONLY_REPOS_URL,
    GitHubDocumentSource,
    Repository,
)

__all__ = [
    "READONLY_REPOS_URL",
    "GitHubDocumentSource",
    "McpSession",
    "McpToolClient",
    "Repository",
    "ToolContract",
    "fingerprint",
    "http_endpoint",
    "verify_url",
]
