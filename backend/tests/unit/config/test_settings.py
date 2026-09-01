"""Tests for configuration parsing and startup validation."""

import pytest
from pydantic import ValidationError

from paimon.config import (
    Environment,
    Settings,
    get_settings,
    unknown_environment_variables,
)

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

    def test_unprefixed_variables_are_ignored(self) -> None:
        environ = {**BASE_ENV, "PATH": "/usr/bin", "OTHER_APP_HOST": "x"}
        assert unknown_environment_variables(environ) == frozenset()

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

    def test_local_environment_allows_them(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = build(monkeypatch, PAIMON_DEBUG="true", PAIMON_DATABASE__ECHO_SQL="true")
        assert settings.debug is True
        assert settings.environment.is_deployed is False
