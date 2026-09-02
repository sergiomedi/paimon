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
    observability: ObservabilitySettings = ObservabilitySettings()

    @model_validator(mode="after")
    def _reject_unsafe_deployed_configuration(self) -> Self:
        """Refuse to start a deployed process with development affordances enabled.

        An authentication bypass that can be switched on by a stray environment
        variable is a vulnerability, not a convenience.
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
        return self


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
        name for name in present if name.startswith(prefix.upper()) and name not in known
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
