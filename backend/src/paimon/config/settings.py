"""Typed application settings, validated at startup.

Configuration is read from the environment with the ``PAIMON_`` prefix and a
double underscore separating nested sections, so ``PAIMON_DATABASE__HOST`` fills
:attr:`Settings.database`.``host``.

Every value is validated when the settings object is built, which happens during
application startup rather than on first use. A misconfigured deployment must
fail immediately and visibly, not hours later on the first request that happens
to touch the misconfigured component.
"""

import os
from collections.abc import Mapping
from enum import StrEnum
from functools import lru_cache
from typing import Literal, Self

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    """Deployment environment the process is running in."""

    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_deployed(self) -> bool:
        """Whether this environment is reachable by someone other than a developer."""
        return self in {Environment.STAGING, Environment.PRODUCTION}


class DatabaseSettings(BaseModel):
    """PostgreSQL connection and pool configuration.

    Two pools are configured rather than one. HTTP handlers hold a connection for
    milliseconds; an agent graph can hold one for minutes. Sharing a single pool
    lets a handful of concurrent agent runs starve the API, and the symptom —
    "the API is slow" — points nowhere useful. Sizing them separately keeps agent
    workload pressure contained, at the cost of two numbers to tune instead of one.

    The sum of ``pool_size + max_overflow + agent_pool_size`` per process, times
    the number of processes, must stay below the server's ``max_connections``.
    """

    host: str
    port: int = Field(default=5432, ge=1, le=65535)
    user: str
    password: SecretStr
    name: str

    pool_size: int = Field(default=10, ge=1, description="Connections for HTTP request handling.")
    max_overflow: int = Field(default=5, ge=0, description="Burst capacity above pool_size.")
    agent_pool_size: int = Field(default=5, ge=1, description="Connections reserved for agents.")
    pool_timeout_seconds: float = Field(default=10.0, gt=0)
    echo_sql: bool = False

    @property
    def dsn(self) -> str:
        """Async SQLAlchemy connection string."""
        password = self.password.get_secret_value()
        return f"postgresql+asyncpg://{self.user}:{password}@{self.host}:{self.port}/{self.name}"

    @property
    def total_connections(self) -> int:
        """Maximum connections a single process can hold open."""
        return self.pool_size + self.max_overflow + self.agent_pool_size


class RedisSettings(BaseModel):
    """Redis connection configuration.

    Everything stored in Redis is derivable or expendable: an embedding cache,
    rate-limit counters and, from Phase 3, agent checkpoints. It is never a system
    of record, so eviction is an accepted outcome rather than data loss.
    """

    host: str
    port: int = Field(default=6379, ge=1, le=65535)
    db: int = Field(default=0, ge=0)
    password: SecretStr | None = None
    use_tls: bool = False
    socket_timeout_seconds: float = Field(default=5.0, gt=0)

    @property
    def url(self) -> str:
        """Redis connection URL."""
        scheme = "rediss" if self.use_tls else "redis"
        credentials = f":{self.password.get_secret_value()}@" if self.password else ""
        return f"{scheme}://{credentials}{self.host}:{self.port}/{self.db}"


TEST_HARNESS_PREFIX = "PAIMON_TEST_"
"""Namespace for variables that configure the test harness, not the service.

The unknown-variable guard would otherwise reject them: it cannot tell a typo
from a deliberate non-setting, and it should not have to know which names the
test suite uses. Reserving a prefix keeps the guard strict about everything it
is actually responsible for.
"""

MAX_INDEXABLE_DIMENSIONS = 2000
"""pgvector indexes the vector type with HNSW only up to this width (ADR-0011)."""

MIN_DEV_SIGNING_KEY_BYTES = 32
"""Minimum HMAC key length for HS256, per RFC 7518 section 3.2."""


class AuthSettings(BaseModel):
    """Identity provider configuration (ADR-0004).

    The platform validates tokens; it never issues or stores credentials. The
    ``dev`` provider signs tokens locally so that development and tests do not
    require tenant connectivity, and :class:`Settings` refuses to start with it
    outside local and test environments.
    """

    provider: Literal["entra", "dev"] = "entra"
    tenant_id: str | None = None
    audience: str | None = Field(default=None, description="Expected 'aud' claim, the client id.")
    jwks_cache_seconds: int = Field(default=3600, ge=60)
    leeway_seconds: int = Field(default=30, ge=0, description="Clock-skew tolerance.")
    dev_signing_key: SecretStr | None = None

    @property
    def jwks_uri(self) -> str:
        """The tenant's JSON Web Key Set endpoint."""
        if self.tenant_id is None:  # pragma: no cover - unreachable after validation
            msg = "jwks_uri requires a tenant id"
            raise ValueError(msg)
        return f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys"

    @model_validator(mode="after")
    def _require_provider_configuration(self) -> Self:
        if self.provider == "entra" and not (self.tenant_id and self.audience):
            msg = "auth.tenant_id and auth.audience are required when provider is 'entra'"
            raise ValueError(msg)
        if self.provider == "dev":
            if self.dev_signing_key is None:
                msg = "auth.dev_signing_key is required when provider is 'dev'"
                raise ValueError(msg)
            # RFC 7518 §3.2: an HMAC key shorter than the hash output weakens
            # HS256. Enforced even for development, because a weak key that only
            # ever lives in development still teaches the wrong default.
            if len(self.dev_signing_key.get_secret_value().encode()) < MIN_DEV_SIGNING_KEY_BYTES:
                msg = (
                    f"auth.dev_signing_key must be at least "
                    f"{MIN_DEV_SIGNING_KEY_BYTES} bytes (RFC 7518 section 3.2)"
                )
                raise ValueError(msg)
        return self


class EmbeddingSettings(BaseModel):
    """Which embedding endpoint to call, and how.

    ``dimensions`` is fixed at 1024 platform-wide (ADR-0011) and is not meant to
    be tuned per deployment: it is the width the index was built on, and changing
    it requires a migration and a full reindex. It appears here so the value can
    be asserted rather than assumed, and so a deployment pointed at the wrong
    model fails at startup instead of writing vectors the index will refuse.
    """

    provider: Literal["local", "azure"] = "local"
    base_url: str = "http://localhost:11434/v1"
    model: str = "bge-m3"
    dimensions: int = Field(default=1024, ge=1, le=2000)
    api_key: SecretStr | None = None
    # Asymmetric models expect an instruction on the query side only. Empty by
    # default because a prefix that the model was not trained with hurts.
    document_prefix: str = ""
    query_prefix: str = ""
    batch_size: int = Field(default=96, ge=1)
    timeout_seconds: float = Field(default=30.0, gt=0)
    index_name: str = "chunks"


class AzureOpenAISettings(BaseModel):
    """An Azure OpenAI resource and its deployments.

    Deployments, not models: Azure lets a deployment be called anything, the URL
    uses that name, and it cannot be inferred from the model. The api-version is
    pinned rather than left to the service, because a version change alters
    response shapes.
    """

    endpoint: str | None = None
    api_key: SecretStr | None = None
    api_version: str = "2024-10-21"
    embedding_deployment: str | None = None
    chat_deployment: str | None = None


class AzureSearchSettings(BaseModel):
    """An Azure AI Search service and the index it holds."""

    endpoint: str | None = None
    api_key: SecretStr | None = None
    index_name: str = "paimon-chunks"
    api_version: str = "2024-07-01"
    # A capability pgvector has no equivalent for. Off unless a configuration
    # exists on the index, so the difference between backends stays visible.
    semantic_configuration: str | None = None


class ChatSettings(BaseModel):
    """Which generation endpoint to call, and how."""

    provider: Literal["local", "azure"] = "local"
    base_url: str = "http://localhost:11434/v1"
    model: str = "qwen2.5:7b-instruct"
    api_key: SecretStr | None = None
    # Zero by default: a grounded answer should be the same answer for the same
    # sources, and an evaluation over a sampled model measures the sampler as
    # much as the retrieval.
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_output_tokens: int | None = Field(default=1024, ge=1)
    timeout_seconds: float = Field(default=120.0, gt=0)


class IngestionSettings(BaseModel):
    """How documents are cut up before indexing."""

    max_chunk_tokens: int = Field(default=512, ge=32)
    chunk_overlap_tokens: int = Field(default=64, ge=0)
    min_chunk_tokens: int = Field(default=16, ge=1)

    @model_validator(mode="after")
    def _overlap_must_leave_room(self) -> Self:
        if self.chunk_overlap_tokens >= self.max_chunk_tokens:
            msg = "chunk_overlap_tokens must leave room for new content"
            raise ValueError(msg)
        if self.min_chunk_tokens > self.max_chunk_tokens:
            msg = "min_chunk_tokens cannot exceed max_chunk_tokens"
            raise ValueError(msg)
        return self


class RetrievalSettings(BaseModel):
    """How much is retrieved, and how much of it reaches the prompt."""

    store: Literal["pgvector", "azure_search"] = "pgvector"
    top_k: int = Field(default=8, ge=1)
    candidates_per_retriever: int = Field(default=40, ge=1)
    rrf_k: int = Field(default=60, ge=0)
    max_context_tokens: int = Field(default=6000, ge=256)

    @model_validator(mode="after")
    def _cannot_return_more_than_is_gathered(self) -> Self:
        if self.candidates_per_retriever < self.top_k:
            msg = "candidates_per_retriever cannot be smaller than top_k"
            raise ValueError(msg)
        return self


class ObservabilitySettings(BaseModel):
    """Logging and, from Phase 5, tracing configuration."""

    service_name: str = "paimon-api"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"


class Settings(BaseSettings):
    """Root settings object, built once per process."""

    model_config = SettingsConfigDict(
        env_prefix="PAIMON_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        # Unknown PAIMON_ variables are a configuration error, not something to
        # ignore: a typo in a deployment variable should stop the process, not
        # silently leave a default in place.
        extra="forbid",
        frozen=True,
    )

    environment: Environment
    debug: bool = False

    database: DatabaseSettings
    redis: RedisSettings
    auth: AuthSettings
    embedding: EmbeddingSettings = EmbeddingSettings()
    azure_openai: AzureOpenAISettings = AzureOpenAISettings()
    azure_search: AzureSearchSettings = AzureSearchSettings()
    chat: ChatSettings = ChatSettings()
    ingestion: IngestionSettings = IngestionSettings()
    retrieval: RetrievalSettings = RetrievalSettings()
    observability: ObservabilitySettings = ObservabilitySettings()

    @model_validator(mode="after")
    def _require_azure_configuration(self) -> Self:
        """Refuse to start with an Azure provider that has nowhere to call.

        Caught here rather than on the first request: a deployment that starts
        and then fails every query looks like an outage, while one that refuses
        to start names the missing setting.
        """
        if self.embedding.provider == "azure" and not (
            self.azure_openai.endpoint and self.azure_openai.embedding_deployment
        ):
            msg = "embedding.provider is 'azure' but azure_openai endpoint/deployment are unset"
            raise ValueError(msg)
        if self.chat.provider == "azure" and not (
            self.azure_openai.endpoint and self.azure_openai.chat_deployment
        ):
            msg = "chat.provider is 'azure' but azure_openai endpoint/chat_deployment are unset"
            raise ValueError(msg)
        if self.retrieval.store == "azure_search" and not self.azure_search.endpoint:
            msg = "retrieval.store is 'azure_search' but azure_search.endpoint is unset"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _reject_unsafe_deployed_configuration(self) -> Self:
        """Refuse to start a deployed process with development affordances enabled.

        An authentication bypass that can be switched on by a stray environment
        variable is a vulnerability, not a convenience.

        The shipped credentials are refused for the same reason. ``.env.example``
        carries values that make a fresh clone work against the local Compose
        stack, which is exactly what makes them dangerous: they are published, so
        a deployment that inherits one is a deployment with a public password.
        """
        if self.environment.is_deployed:
            if self.auth.provider == "dev":
                msg = f"the 'dev' identity provider is not allowed in {self.environment}"
                raise ValueError(msg)
            if self.debug:
                msg = f"debug mode is not allowed in {self.environment}"
                raise ValueError(msg)
            if self.database.echo_sql:
                msg = f"SQL echo leaks query parameters and is not allowed in {self.environment}"
                raise ValueError(msg)
            if self.database.password.get_secret_value() in SHIPPED_CREDENTIALS:
                msg = (
                    "the database password is one of the values shipped in "
                    f".env.example, which is public; {self.environment} needs its own"
                )
                raise ValueError(msg)
            signing_key = self.auth.dev_signing_key
            if signing_key and signing_key.get_secret_value() in SHIPPED_CREDENTIALS:
                msg = (
                    "the development signing key shipped in .env.example is public "
                    f"and is not allowed in {self.environment}"
                )
                raise ValueError(msg)
        return self


SHIPPED_CREDENTIALS = frozenset(
    {
        "paimon",
        "change-me",
        "local-development-only-not-a-real-secret-value",
    }
)
"""Secrets that appear in ``.env.example`` and are therefore not secret.

Listed rather than pattern-matched: a heuristic for "looks like a placeholder"
either rejects a legitimate password or misses one of these, and both failures
are worse than a list that has to be kept in step with one file.
"""


def _known_environment_variables() -> frozenset[str]:
    """Every environment variable name the settings model can consume.

    Covers the one level of nesting the settings actually use; a deeper model
    would need this to recurse.
    """
    prefix = Settings.model_config.get("env_prefix", "")
    delimiter = Settings.model_config.get("env_nested_delimiter", "")
    names: set[str] = set()
    for name, field in Settings.model_fields.items():
        base = f"{prefix}{name}".upper()
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            names.update(f"{base}{delimiter}{sub}".upper() for sub in annotation.model_fields)
        else:
            names.add(base)
    return frozenset(names)


def unknown_environment_variables(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    """Return prefixed variables that map to no settings field.

    pydantic-settings reads only the variables it recognises, so ``extra="forbid"``
    never sees the others: ``PAIMON_DATABSE__HOST`` would be silently ignored and
    the service would start on a default. This closes that gap explicitly.

    Args:
        environ: Environment to inspect. Defaults to the process environment.

    Returns:
        The offending variable names, empty when the environment is clean.
    """
    prefix = Settings.model_config.get("env_prefix", "")
    known = _known_environment_variables()
    present = {name.upper() for name in (environ if environ is not None else os.environ)}
    return frozenset(
        name
        for name in present
        if name.startswith(prefix.upper())
        and not name.startswith(TEST_HARNESS_PREFIX)
        and name not in known
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, building and validating them on first call.

    Cached because settings are immutable for the lifetime of the process. Tests
    that need a different configuration construct :class:`Settings` directly
    rather than clearing this cache.

    Raises:
        ValueError: If the environment contains prefixed variables that match no
            settings field, which almost always means a typo in a deployment.
    """
    if unknown := unknown_environment_variables():
        listed = ", ".join(sorted(unknown))
        msg = f"unknown configuration variables (check for typos): {listed}"
        raise ValueError(msg)
    return Settings()
