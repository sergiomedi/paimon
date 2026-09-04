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
    def psycopg_dsn(self) -> str:
        """Connection string for the graph checkpointer.

        The same database as :attr:`dsn`, reached by psycopg rather than asyncpg
        (ADR-0017). Derived from the same fields rather than configured
        separately: two connection strings for one database is one more chance
        for a deployment to point half of itself somewhere else.
        """
        password = self.password.get_secret_value()
        return f"postgresql://{self.user}:{password}@{self.host}:{self.port}/{self.name}"

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


class AgentSettings(BaseModel):
    """How agent runs behave."""

    # Off by default: resumable runs cost a second connection pool on a second
    # driver, and a deployment that never suspends a run should not pay for it.
    resumable: bool = False
    step_limit: int = Field(default=25, ge=1, le=200)
    # The one agent whose output an organization publishes, so the one where
    # waiting for a person is worth it when someone asks for it.
    review_postmortems: bool = False


class McpSettings(BaseModel):
    """Whether this deployment speaks the Model Context Protocol, and where."""

    enabled: bool = True
    # Mounted inside the API rather than run as its own service. One deployment
    # unit, one authentication path, one set of pools — and the cost is that MCP
    # traffic and API traffic share them, which is a Phase 7 problem once there
    # are numbers to size it with.
    path: str = "/mcp"
    # Host header values this server answers to. The transport rejects anything
    # else, which is protection against DNS rebinding: a browser page can be
    # made to resolve an attacker's hostname to 127.0.0.1 and then talk to a
    # local server, and the Host header is what gives that away. The defaults
    # cover local development; a deployment behind a proxy must list its own
    # names, and an empty list would refuse every request rather than allow any.
    allowed_hosts: tuple[str, ...] = (
        "localhost",
        "127.0.0.1",
        "localhost:8000",
        "127.0.0.1:8000",
    )
    # Browser Origin values permitted to reach the endpoint. Empty by default:
    # this is an API for programs, and a page that wants to call it directly is
    # the case the rebinding protection exists to catch.
    allowed_origins: tuple[str, ...] = ()
    # The canonical URI clients name when asking their authorization server for
    # a token for this server (RFC 8707). It has to be the address clients
    # actually reach — behind a proxy that is the public one, not the container's
    # — because a token minted for one audience and checked against another is
    # rejected with no useful explanation on either side.
    resource_url: str | None = None
    # Where tokens for it come from. Unset means this deployment does not publish
    # protected resource metadata and authenticates inside the call instead,
    # which is what the dev identity provider does: it is not an OAuth issuer and
    # advertising one that does not exist would send clients somewhere useless.
    authorization_server: str | None = None

    @property
    def publishes_metadata(self) -> bool:
        """Whether this deployment can describe itself as a protected resource."""
        return bool(self.resource_url and self.authorization_server)

    @model_validator(mode="after")
    def _hosts_must_be_listed(self) -> Self:
        if self.enabled and not self.allowed_hosts:
            msg = "mcp.allowed_hosts cannot be empty: it would refuse every request"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _path_must_be_absolute(self) -> Self:
        if not self.path.startswith("/") or self.path == "/":
            msg = "mcp.path must be an absolute sub-path, for example '/mcp'"
            raise ValueError(msg)
        return self


class GitHubSourceSettings(BaseModel):
    """One repository this deployment synchronises from GitHub.

    Attributes:
        name: How this source is named in a request and in logs.
        owner: Account or organization.
        repo: Repository name.
        paths: Directories to walk. Empty means the repository root, which is
            almost never what you want on a repository that also holds code.
        ref: Branch, tag or commit. Unset follows the default branch.
        suffixes: Extensions to index.
        max_depth: How deep the walk descends.
        max_documents: Ceiling on one synchronisation.
        pinned_tools: Fingerprints of the server's tool definitions, as recorded
            when they were reviewed. Empty is allowed, and the first connection
            logs the digests to put here.
    """

    name: str
    owner: str
    repo: str
    paths: tuple[str, ...] = ()
    ref: str | None = None
    suffixes: tuple[str, ...] = (".md",)
    max_depth: int = 4
    max_documents: int = 200
    pinned_tools: Mapping[str, str] = Field(default_factory=dict)


class SourcesSettings(BaseModel):
    """External systems this deployment reads documents from.

    A registry, not a parameter. The endpoint that starts a synchronisation
    names a source configured here; it does not accept a URL, a repository or a
    credential from the caller. That is the difference between an integration
    and a server-side request forgery gadget, and it is a property of where the
    values come from rather than of anything that could be validated on the way
    in.

    Attributes:
        github_endpoint: The MCP server GitHub sources talk to. The **read-only**
            repository toolset by default (the same URL the adapter names as
            ``READONLY_REPOS_URL``): a synchronisation has no business being able
            to call ``delete_file``, and scoping it here means it cannot,
            whatever it is later asked to do.
        github_token: Credential presented to that server.
        github: The repositories this deployment offers.
        allow_loopback_endpoints: Permit an ``http://localhost`` MCP server.
            Refused outside local and test environments, because the guard it
            switches off is the one that stops a client running inside a server
            from being pointed back at that server.
    """

    github_endpoint: str = "https://api.githubcopilot.com/mcp/x/repos/readonly"
    github_token: SecretStr | None = None
    github: tuple[GitHubSourceSettings, ...] = ()
    allow_loopback_endpoints: bool = False

    @model_validator(mode="after")
    def _names_must_be_unique(self) -> Self:
        names = [source.name for source in self.github]
        duplicated = {name for name in names if names.count(name) > 1}
        if duplicated:
            msg = f"source names must be unique; repeated: {', '.join(sorted(duplicated))}"
            raise ValueError(msg)
        return self


class TracingSettings(BaseModel):
    """Where traces go, and how much of a request they are allowed to carry.

    Attributes:
        enabled: Whether to build a real tracer at all. Off by default: a
            deployment with nowhere to send traces should pay nothing for them,
            and the platform's own no-op tracer costs less than an exporter that
            drops what it collects.
        endpoint: OTLP/HTTP collector to export to. Any backend that speaks OTLP
            — Langfuse, Azure Monitor, a collector in front of either — is a
            change to this value and nothing else (ADR-0025).
        headers: Sent with every export. This is where a backend's credential
            goes; Langfuse, for instance, wants HTTP basic auth here.
        sample_ratio: Fraction of traces to keep, between 0 and 1. Head-based:
            the decision is made once at the root and inherited, so a sampled
            trace is complete rather than full of holes.
        capture_content: Record prompts and completions on spans. **Off, and
            refused outright in deployed environments.** The semantic conventions
            mark message content opt-in and warn it is likely to contain
            sensitive data; here that content is an organization's internal
            documentation and whatever its people typed. Turning it on where real
            tenants' material flows should cost a code change and a review, and
            this guard is what makes it cost one — an environment variable is too
            easy a way to start exporting somebody's runbooks.
        export_timeout_seconds: How long an export may take before it is
            abandoned. A collector that stops answering must not become this
            platform's outage.
    """

    enabled: bool = False
    endpoint: str | None = None
    headers: Mapping[str, str] = Field(default_factory=dict)
    sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    capture_content: bool = False
    export_timeout_seconds: float = Field(default=10.0, gt=0.0)

    @model_validator(mode="after")
    def _enabled_needs_somewhere_to_send(self) -> Self:
        if self.enabled and not self.endpoint:
            msg = "tracing.enabled is true but tracing.endpoint is unset: there is nowhere to send"
            raise ValueError(msg)
        return self


class ModelPrice(BaseModel):
    """What one model costs, per million tokens.

    Per million rather than per token because that is the unit every provider
    publishes, and converting at the point of configuration is where a factor of
    a thousand goes unnoticed.

    Attributes:
        input: Price per million input tokens, in the currency below.
        output: Price per million output tokens.
    """

    input: float = Field(ge=0.0)
    output: float = Field(ge=0.0)


class PricingSettings(BaseModel):
    """A price list, and what it is worth trusting for.

    Cost is **not** in the semantic conventions and is not measured: it is token
    counts multiplied by a table somebody typed. The provider's invoice is the
    authority. What this buys is the shape of a bill before it arrives — which
    tenant, which model, which day — and that is worth having as long as nobody
    mistakes it for the bill.

    Attributes:
        currency: What the prices are denominated in. Recorded on every
            measurement, because a chart mixing two currencies is worse than no
            chart.
        revision: A label for this table — a date, a version, anything stable.
            Recorded on every measurement so that a number can be traced back to
            the prices that produced it. Without it, last month's figures are
            uninterpretable the moment the table changes.
        models: Price per model id, as the provider reports the model. A model
            absent from the table produces no cost measurement rather than a
            zero: zero is a claim, and the honest answer is silence.
    """

    currency: str = "USD"
    revision: str = "unset"
    models: Mapping[str, ModelPrice] = Field(default_factory=dict)

    def cost(self, model: str, *, input_tokens: int, output_tokens: int) -> float | None:
        """Estimate what one call cost, or None if the model is unpriced."""
        price = self.models.get(model)
        if price is None:
            return None
        return (input_tokens * price.input + output_tokens * price.output) / 1_000_000


class MetricsSettings(BaseModel):
    """Where measurements go.

    Separate from tracing on purpose, and this is a trap worth naming: several
    tracing backends — Langfuse among them — accept OTLP **traces** and nothing
    else. Pointing metrics at a traces endpoint fails quietly, and the symptom is
    an empty dashboard that looks like a platform emitting nothing. Metrics
    usually want a collector, or a backend that takes them.

    Attributes:
        enabled: Whether to export measurements at all.
        endpoint: OTLP/HTTP metrics collector.
        headers: Sent with every export; the backend's credential goes here.
        export_interval_seconds: How often measurements are pushed.
        pricing: The price list, if this deployment has one.
    """

    enabled: bool = False
    endpoint: str | None = None
    headers: Mapping[str, str] = Field(default_factory=dict)
    export_interval_seconds: float = Field(default=60.0, gt=0.0)
    pricing: PricingSettings = PricingSettings()

    @model_validator(mode="after")
    def _enabled_needs_somewhere_to_send(self) -> Self:
        if self.enabled and not self.endpoint:
            msg = "metrics.enabled is true but metrics.endpoint is unset: there is nowhere to send"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _a_price_list_must_say_which_one_it_is(self) -> Self:
        if self.pricing.models and self.pricing.revision == "unset":
            msg = (
                "metrics.pricing.revision must name this price list (a date will do): "
                "a cost figure that cannot be traced back to the prices that produced it "
                "is uninterpretable the moment the table changes"
            )
            raise ValueError(msg)
        return self


class ObservabilitySettings(BaseModel):
    """Logging, tracing and metrics configuration."""

    service_name: str = "paimon-api"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    tracing: TracingSettings = TracingSettings()
    metrics: MetricsSettings = MetricsSettings()


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
    agents: AgentSettings = AgentSettings()
    mcp: McpSettings = McpSettings()
    sources: SourcesSettings = SourcesSettings()
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
            if self.observability.tracing.capture_content:
                msg = (
                    "tracing.capture_content exports prompts and completions — an "
                    "organization's documentation and whatever its people typed — to "
                    f"whoever receives the traces, and is not allowed in {self.environment}"
                )
                raise ValueError(msg)
            if self.sources.allow_loopback_endpoints:
                msg = (
                    "sources.allow_loopback_endpoints disables the guard that stops this "
                    f"process from dialling itself, and is not allowed in {self.environment}"
                )
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
