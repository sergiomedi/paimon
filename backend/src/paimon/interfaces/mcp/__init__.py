"""Paimon exposed over the Model Context Protocol."""

from paimon.interfaces.mcp.auth import (
    RequireBearerToken,
    challenge,
    protected_resource_routes,
)
from paimon.interfaces.mcp.gateway import GatewayFactory, McpToolGateway, bearer_token
from paimon.interfaces.mcp.server import SERVER_NAME, build_mcp_server

__all__ = [
    "SERVER_NAME",
    "GatewayFactory",
    "McpToolGateway",
    "RequireBearerToken",
    "bearer_token",
    "build_mcp_server",
    "challenge",
    "protected_resource_routes",
]
