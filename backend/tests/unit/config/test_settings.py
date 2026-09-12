"""Tests for configuration parsing and startup validation."""

import pytest
from pydantic import ValidationError

from paimon.config import (
    Environment,
    Settings,
    get_settings,
    unknown_environment_variables,
)
from paimon.infrastructure.sources import READONLY_REPOS_URL

BASE_ENV = {
    "PAIMON_ENVIRONMENT": "local",
    "PAIMON_DATABASE__HOST": "db.internal",
    "PAIMON_DATABASE__USER": "paimon",
    "PAIMON_DATABASE__PASSWORD": "s3cret",
    "PAIMON_DATABASE__NAME": "paimon",
    "PAIMON_REDIS__HOST": "cache.internal",
    "PAIMON_AUTH__PROVIDER": "dev",
    "PAIMON_AUTH__DEV_SIGNING_KEY": "local-only-padded-to-thirty-two-bytes",
}


def build(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> Settings:
    for key in list(BASE_ENV) + list(overrides):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**BASE_ENV, **overrides}.items():
        monkeypatch.setenv(key, value)
    # _env_file=None keeps a developer's local .env out of the test run.
    return Settings(_env_file=None)


class TestParsing:
    def test_nested_variables_populate_sections(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = build(monkeypatch)
        assert settings.environment is Environment.LOCAL
        assert settings.database.host == "db.internal"
        assert settings.redis.host == "cache.internal"

    def test_secrets_are_not_exposed_by_repr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = build(monkeypatch)
        assert "s3cret" not in repr(settings)

    def test_dsn_is_built_from_the_parts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = build(monkeypatch)
        assert settings.database.dsn == "postgresql+asyncpg://paimon:s3cret@db.internal:5432/paimon"

    def test_total_connections_covers_both_pools(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = build(
            monkeypatch,
            PAIMON_DATABASE__POOL_SIZE="20",
            PAIMON_DATABASE__MAX_OVERFLOW="10",
            PAIMON_DATABASE__AGENT_POOL_SIZE="8",
        )
        assert settings.database.total_connections == 38

    def test_redis_url_includes_credentials_and_scheme(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = build(
            monkeypatch,
            PAIMON_REDIS__PASSWORD="pw",  # noqa: S106  a literal in a test, not a secret
            PAIMON_REDIS__USE_TLS="true",
        )
        assert settings.redis.url == "rediss://:pw@cache.internal:6379/0"


class TestUnknownVariableDetection:
    """pydantic-settings ignores variables it does not recognise, so a typo would
    otherwise leave the service running on a default. get_settings closes that gap."""

    def test_a_clean_environment_reports_nothing(self) -> None:
        assert unknown_environment_variables(BASE_ENV) == frozenset()

    def test_a_typo_is_reported(self) -> None:
        environ = {**BASE_ENV, "PAIMON_DATABSE__HOST": "typo.internal"}
        assert unknown_environment_variables(environ) == frozenset({"PAIMON_DATABSE__HOST"})

    def test_the_test_harness_namespace_is_ignored(self) -> None:
        """The guard cannot tell a typo from a deliberate non-setting, so the test
        harness gets a reserved prefix rather than the guard getting a list of
        names it should not have to know."""
        environ = {**BASE_ENV, "PAIMON_TEST_REQUIRE_INTEGRATION": "1"}
        assert unknown_environment_variables(environ) == frozenset()

    def test_unprefixed_variables_are_ignored(self) -> None:
        environ = {**BASE_ENV, "PATH": "/usr/bin", "OTHER_APP_HOST": "x"}
        assert unknown_environment_variables(environ) == frozenset()

    @pytest.mark.parametrize(
        "name",
        [
            "PAIMON_ENVIRONMENT",
            "PAIMON_DATABASE",
            "PAIMON_DATABASE__HOST",
            "PAIMON_OBSERVABILITY__TRACING__ENABLED",
            "PAIMON_OBSERVABILITY__TRACING__ENDPOINT",
            "PAIMON_OBSERVABILITY__METRICS__ENDPOINT",
            "PAIMON_OBSERVABILITY__METRICS__PRICING__CURRENCY",
            "PAIMON_OBSERVABILITY__METRICS__PRICING__MODELS",
        ],
    )
    def test_nesting_is_followed_all_the_way_down(self, name: str) -> None:
        """The guard walked one level and the model is four deep.

        Every name here is real and pydantic consumes it; the guard reported the
        deeper ones as typos, and the deployed container therefore exited 1 two
        seconds into every start. A guard that rejects valid configuration is
        worse than no guard: it fails at the moment the thing it protects is
        finally being used, and it blames the wrong thing while doing it.
        """
        assert unknown_environment_variables({**BASE_ENV, name: "x"}) == frozenset()

    def test_a_typo_deep_in_the_model_is_still_reported(self) -> None:
        """Recursing must not turn the guard into one that accepts anything."""
        environ = {**BASE_ENV, "PAIMON_OBSERVABILITY__TRACING__ENDPONT": "x"}
        assert unknown_environment_variables(environ) == frozenset(
            {"PAIMON_OBSERVABILITY__TRACING__ENDPONT"}
        )

    def test_get_settings_refuses_to_build_with_a_typo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        get_settings.cache_clear()
        for key, value in BASE_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("PAIMON_DATABSE__HOST", "typo.internal")
        with pytest.raises(ValueError, match="unknown configuration variables"):
            get_settings()
        get_settings.cache_clear()


class TestAuthValidation:
    def test_entra_requires_tenant_and_audience(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ValidationError, match=r"tenant_id and auth\.audience are required"):
            build(monkeypatch, PAIMON_AUTH__PROVIDER="entra")

    def test_dev_requires_a_signing_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PAIMON_AUTH__DEV_SIGNING_KEY", raising=False)
        env = {k: v for k, v in BASE_ENV.items() if k != "PAIMON_AUTH__DEV_SIGNING_KEY"}
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        with pytest.raises(ValidationError, match="dev_signing_key is required"):
            Settings(_env_file=None)

    def test_a_short_dev_signing_key_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """RFC 7518 section 3.2: an HMAC key shorter than the hash output weakens HS256."""
        with pytest.raises(ValidationError, match="at least 32 bytes"):
            build(monkeypatch, PAIMON_AUTH__DEV_SIGNING_KEY="too-short")

    def test_jwks_uri_is_derived_from_the_tenant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = build(
            monkeypatch,
            PAIMON_AUTH__PROVIDER="entra",
            PAIMON_AUTH__TENANT_ID="tenant-123",
            PAIMON_AUTH__AUDIENCE="api://paimon",
        )
        assert settings.auth.jwks_uri.endswith("/tenant-123/discovery/v2.0/keys")


class TestEmbeddingSettings:
    def test_it_defaults_to_the_platform_width(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert build(monkeypatch).embedding.dimensions == 1024

    def test_a_width_pgvector_cannot_index_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pgvector indexes the vector type with HNSW only up to 2000 dimensions
        (ADR-0011); above that a deployment would silently fall back to scanning
        every chunk."""
        with pytest.raises(ValidationError, match="less than or equal to 2000"):
            build(monkeypatch, PAIMON_EMBEDDING__DIMENSIONS="3072")

    def test_the_endpoint_is_configurable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = build(
            monkeypatch,
            PAIMON_EMBEDDING__BASE_URL="http://models.internal/v1",
            PAIMON_EMBEDDING__MODEL="bge-m3",
            PAIMON_EMBEDDING__QUERY_PREFIX="query: ",
        )
        assert settings.embedding.base_url == "http://models.internal/v1"
        assert settings.embedding.query_prefix == "query: "
        assert settings.embedding.document_prefix == ""


class TestDeployedEnvironmentGuards:
    """A development affordance reachable from production is a vulnerability."""

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_dev_identity_provider_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, environment: str
    ) -> None:
        with pytest.raises(ValidationError, match="'dev' identity provider is not allowed"):
            build(monkeypatch, PAIMON_ENVIRONMENT=environment)

    def test_debug_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ValidationError, match="debug mode is not allowed"):
            build(
                monkeypatch,
                PAIMON_ENVIRONMENT="production",
                PAIMON_DEBUG="true",
                PAIMON_AUTH__PROVIDER="entra",
                PAIMON_AUTH__TENANT_ID="t",
                PAIMON_AUTH__AUDIENCE="a",
            )

    def test_sql_echo_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ValidationError, match="SQL echo leaks query parameters"):
            build(
                monkeypatch,
                PAIMON_ENVIRONMENT="production",
                PAIMON_DATABASE__ECHO_SQL="true",
                PAIMON_AUTH__PROVIDER="entra",
                PAIMON_AUTH__TENANT_ID="t",
                PAIMON_AUTH__AUDIENCE="a",
            )

    def test_capturing_prompt_content_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Prompts and completions are an organization's documentation and
        # whatever its people typed. Turning that export on where real tenants'
        # material flows should cost a code change and a review, and this guard
        # is what makes it cost one.
        with pytest.raises(ValidationError, match="capture_content"):
            build(
                monkeypatch,
                PAIMON_ENVIRONMENT="production",
                PAIMON_OBSERVABILITY__TRACING__CAPTURE_CONTENT="true",
                PAIMON_AUTH__PROVIDER="entra",
                PAIMON_AUTH__TENANT_ID="t",
                PAIMON_AUTH__AUDIENCE="a",
            )

    def test_loopback_source_endpoints_are_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # It switches off the guard that stops a client inside a server from
        # being pointed at private addresses — the metadata endpoint included.
        with pytest.raises(ValidationError, match="allow_loopback_endpoints"):
            build(
                monkeypatch,
                PAIMON_ENVIRONMENT="production",
                PAIMON_SOURCES__ALLOW_LOOPBACK_ENDPOINTS="true",
                PAIMON_AUTH__PROVIDER="entra",
                PAIMON_AUTH__TENANT_ID="t",
                PAIMON_AUTH__AUDIENCE="a",
            )

    def test_local_environment_allows_them(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = build(monkeypatch, PAIMON_DEBUG="true", PAIMON_DATABASE__ECHO_SQL="true")
        assert settings.debug is True
        assert settings.environment.is_deployed is False


class TestAzureProviders:
    """Configuration that points at Azure must be complete before startup."""

    def test_azure_embeddings_without_a_deployment_are_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A deployment that starts and then fails every query looks like an
        outage; one that refuses to start names the missing setting."""
        with pytest.raises(ValidationError, match="azure_openai endpoint/deployment are unset"):
            build(monkeypatch, PAIMON_EMBEDDING__PROVIDER="azure")

    def test_azure_chat_without_a_deployment_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(ValidationError, match="chat_deployment are unset"):
            build(
                monkeypatch,
                PAIMON_CHAT__PROVIDER="azure",
                PAIMON_AZURE_OPENAI__ENDPOINT="https://r.openai.azure.com",
            )

    def test_azure_search_without_an_endpoint_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(ValidationError, match=r"azure_search\.endpoint is unset"):
            build(monkeypatch, PAIMON_RETRIEVAL__STORE="azure_search")

    def test_a_complete_azure_configuration_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = build(
            monkeypatch,
            PAIMON_EMBEDDING__PROVIDER="azure",
            PAIMON_CHAT__PROVIDER="azure",
            PAIMON_RETRIEVAL__STORE="azure_search",
            PAIMON_AZURE_OPENAI__ENDPOINT="https://r.openai.azure.com",
            PAIMON_AZURE_OPENAI__EMBEDDING_DEPLOYMENT="embed-prod",
            PAIMON_AZURE_OPENAI__CHAT_DEPLOYMENT="chat-prod",
            PAIMON_AZURE_SEARCH__ENDPOINT="https://s.search.windows.net",
        )
        assert settings.azure_openai.embedding_deployment == "embed-prod"
        assert settings.azure_search.index_name == "paimon-chunks"

    def test_the_local_providers_need_no_azure_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = build(monkeypatch)
        assert settings.embedding.provider == "local"
        assert settings.retrieval.store == "pgvector"
        assert settings.azure_openai.endpoint is None


DEPLOYED = {
    "PAIMON_ENVIRONMENT": "production",
    "PAIMON_AUTH__PROVIDER": "entra",
    "PAIMON_AUTH__TENANT_ID": "t",
    "PAIMON_AUTH__AUDIENCE": "a",
}


class TestShippedCredentialsAreRefusedWhenDeployed:
    """The values in .env.example are published, so they are not secrets.

    The point is not that they are weak strings. It is that anyone who has read
    the repository knows them, which is exactly what makes a deployment that
    inherits one indefensible.
    """

    def test_the_shipped_database_password_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(ValidationError, match="shipped in"):
            build(monkeypatch, **DEPLOYED, PAIMON_DATABASE__PASSWORD="paimon")  # noqa: S106

    def test_the_older_placeholder_is_refused_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # It was the shipped value until this batch, so a deployment set up from
        # an earlier clone still carries it.
        with pytest.raises(ValidationError, match="shipped in"):
            build(monkeypatch, **DEPLOYED, PAIMON_DATABASE__PASSWORD="change-me")  # noqa: S106

    def test_a_password_of_its_own_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = build(
            monkeypatch,
            **DEPLOYED,
            PAIMON_DATABASE__PASSWORD="a-value-this-repository-never-published",  # noqa: S106
        )
        assert settings.environment.is_deployed

    def test_the_same_password_is_fine_locally(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Scoped to deployed environments: locally the published value costs
        # nothing, and refusing it would break the clone-and-run path the
        # example file exists to provide.
        settings = build(monkeypatch, PAIMON_DATABASE__PASSWORD="paimon")  # noqa: S106
        assert settings.database.password is not None
        assert settings.database.password.get_secret_value() == "paimon"


class TestSourceRegistry:
    """Sources are configuration, and the registry has to be unambiguous."""

    def test_repeated_source_names_are_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Two sources under one name means a caller asking for it gets whichever
        # the dict kept, which is a coin toss written in a config file.
        duplicated = (
            '[{"name":"docs","owner":"a","repo":"b"},{"name":"docs","owner":"c","repo":"d"}]'
        )
        with pytest.raises(ValidationError, match="source names must be unique"):
            build(monkeypatch, PAIMON_SOURCES__GITHUB=duplicated)

    def test_the_default_endpoint_is_the_read_only_toolset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pinned against the adapter's own constant, so the two cannot drift into
        # a default that quietly grants write access.
        assert build(monkeypatch).sources.github_endpoint == READONLY_REPOS_URL

    def test_a_configured_source_is_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = build(
            monkeypatch,
            PAIMON_SOURCES__GITHUB='[{"name":"handbook","owner":"acme","repo":"h",'
            '"paths":["docs"],"pinned_tools":{"get_file_contents":"abc"}}]',
        )
        source = settings.sources.github[0]
        assert source.name == "handbook"
        assert source.paths == ("docs",)
        assert source.pinned_tools["get_file_contents"] == "abc"


class TestTracingSettings:
    def test_tracing_is_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = build(monkeypatch)
        assert settings.observability.tracing.enabled is False
        assert settings.observability.tracing.capture_content is False

    def test_a_nested_tracing_variable_reaches_the_right_field(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = build(
            monkeypatch,
            PAIMON_OBSERVABILITY__TRACING__ENABLED="true",
            PAIMON_OBSERVABILITY__TRACING__ENDPOINT="https://collector.test/v1/traces",
            PAIMON_OBSERVABILITY__TRACING__SAMPLE_RATIO="0.25",
        )
        assert settings.observability.tracing.endpoint == "https://collector.test/v1/traces"
        assert settings.observability.tracing.sample_ratio == 0.25


class TestTheJudge:
    """A model grading its own homework is the failure this guard exists for."""

    def test_a_judge_that_is_the_generator_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Models score their own family more generously, by an unknown amount.
        # Unknown is the problem: a known bias could be subtracted.
        with pytest.raises(ValidationError, match="same endpoint"):
            build(
                monkeypatch,
                PAIMON_EVALUATION__JUDGE__ENABLED="true",
                PAIMON_EVALUATION__JUDGE__MODEL="qwen2.5:7b-instruct",
            )

    def test_a_different_model_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = build(
            monkeypatch,
            PAIMON_EVALUATION__JUDGE__ENABLED="true",
            PAIMON_EVALUATION__JUDGE__MODEL="llama3.1:8b-instruct",
        )
        assert settings.evaluation.judge.model == "llama3.1:8b-instruct"

    def test_self_judging_can_be_accepted_knowingly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A deployment with one model available is a real situation. Saying so is
        # the price, and the report then says so too.
        settings = build(
            monkeypatch,
            PAIMON_EVALUATION__JUDGE__ENABLED="true",
            PAIMON_EVALUATION__JUDGE__MODEL="qwen2.5:7b-instruct",
            PAIMON_EVALUATION__JUDGE__ACKNOWLEDGE_SELF_JUDGING="true",
        )
        assert settings.evaluation.judge.acknowledge_self_judging

    def test_a_judge_without_a_model_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ValidationError, match="nobody to ask"):
            build(monkeypatch, PAIMON_EVALUATION__JUDGE__ENABLED="true")

    def test_an_even_number_of_samples_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Ties are recorded as undecided rather than broken towards the generous
        # label, so an even number of samples just wastes calls producing them.
        with pytest.raises(ValidationError, match="even number of samples"):
            build(
                monkeypatch,
                PAIMON_EVALUATION__JUDGE__ENABLED="true",
                PAIMON_EVALUATION__JUDGE__MODEL="llama3.1:8b-instruct",
                PAIMON_EVALUATION__JUDGE__SAMPLES="2",
            )

    def test_judging_is_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The verified metrics need no model, and a benchmark that silently costs
        # money per run is a benchmark people stop running.
        assert build(monkeypatch).evaluation.judge.enabled is False


class TestDatabaseAuthentication:
    """A password and a token are alternatives, not a pair."""

    def test_entra_needs_no_password(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An emptied-out variable is the realistic shape of "we moved to Entra",
        # and it is not a credential by any reading.
        settings = build(
            monkeypatch,
            PAIMON_DATABASE__AUTH="entra",
            PAIMON_DATABASE__PASSWORD="",
            PAIMON_DATABASE__USER="id-paimon-dev",
        )
        assert settings.database.auth == "entra"
        assert settings.database.password is None
        credentials = settings.database.dsn.split("//")[1].split("@")[0]
        assert credentials == "id-paimon-dev"

    def test_resumable_agents_are_refused_with_entra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The one combination that fails hours after it starts working.

        The graph checkpointer takes a connection string built once at startup.
        A token in it is valid until it is not, and the failure arrives as
        authentication errors on a feature that passed its tests.
        """
        with pytest.raises(ValidationError, match=r"agents\.resumable cannot be used"):
            build(
                monkeypatch,
                PAIMON_DATABASE__AUTH="entra",
                PAIMON_DATABASE__PASSWORD="",
                PAIMON_AGENTS__RESUMABLE="true",
            )
