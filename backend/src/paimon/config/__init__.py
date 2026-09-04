"""Application configuration.

Settings are read once, validated at startup and then treated as immutable. This
package sits outside the architectural layers: it is read by the composition root
and by infrastructure adapters, never by the domain or by use cases, which
receive what they need as explicit arguments.
"""

from paimon.config.settings import (
    AgentSettings,
    AuthSettings,
    AzureOpenAISettings,
    AzureSearchSettings,
    ChatSettings,
    DatabaseSettings,
    EmbeddingSettings,
    Environment,
    GitHubSourceSettings,
    IngestionSettings,
    McpSettings,
    MetricsSettings,
    ModelPrice,
    ObservabilitySettings,
    PricingSettings,
    RedisSettings,
    RetrievalSettings,
    Settings,
    SourcesSettings,
    TracingSettings,
    get_settings,
    unknown_environment_variables,
)

__all__ = [
    "AgentSettings",
    "AuthSettings",
    "AzureOpenAISettings",
    "AzureSearchSettings",
    "ChatSettings",
    "DatabaseSettings",
    "EmbeddingSettings",
    "Environment",
    "GitHubSourceSettings",
    "IngestionSettings",
    "McpSettings",
    "MetricsSettings",
    "ModelPrice",
    "ObservabilitySettings",
    "PricingSettings",
    "RedisSettings",
    "RetrievalSettings",
    "Settings",
    "SourcesSettings",
    "TracingSettings",
    "get_settings",
    "unknown_environment_variables",
]
