"""Azure adapters and the credentials they authenticate with."""

from paimon.infrastructure.azure.credentials import (
    ApiKeyCredential,
    AzureCredential,
    EntraCredential,
    build_credential,
)

__all__ = ["ApiKeyCredential", "AzureCredential", "EntraCredential", "build_credential"]
