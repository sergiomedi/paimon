"""Paimon exposed over the Model Context Protocol."""

from paimon.interfaces.mcp.auth import (
    RequireBearerToken,
    challenge,
    protected_resource_routes,
)
from paimon.interfaces.mcp.discovery import (
    DISCOVERY_PATH,
    SERVER_ID,
    discovery_routes,
    server_json,
)
from paimon.interfaces.mcp.gateway import GatewayFactory, McpToolGateway, bearer_token
from paimon.interfaces.mcp.server import SERVER_NAME, build_mcp_server

__all__ = [
    "DISCOVERY_PATH",
    "SERVER_ID",
    "SERVER_NAME",
    "GatewayFactory",
    "McpToolGateway",
    "RequireBearerToken",
    "bearer_token",
    "build_mcp_server",
    "challenge",
    "discovery_routes",
    "protected_resource_routes",
    "server_json",
]
