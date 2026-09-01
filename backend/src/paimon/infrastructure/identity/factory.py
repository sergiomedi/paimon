"""Selection of the identity adapter from configuration."""

from paimon.config import AuthSettings, Environment
from paimon.domain.ports import IdentityProvider
from paimon.infrastructure.identity.dev import DevIdentityProvider
from paimon.infrastructure.identity.entra import EntraIdentityProvider


def build_identity_provider(settings: AuthSettings, environment: Environment) -> IdentityProvider:
    """Build the identity adapter configured for this environment.

    Args:
        settings: Identity configuration.
        environment: The environment the process is running in.

    Returns:
        The adapter to bind to the IdentityProvider port.

    Raises:
        ValueError: If the development signer is requested in a deployed
            environment, or required configuration is missing.
    """
    if settings.provider == "dev":
        # Settings already rejects this combination at startup. The check is
        # repeated here because this factory can be called directly, and an
        # authentication bypass is worth guarding twice.
        if environment.is_deployed:
            msg = f"the development identity provider is not allowed in {environment}"
            raise ValueError(msg)
        if settings.dev_signing_key is None:  # pragma: no cover - validated in AuthSettings
            msg = "the development identity provider requires a signing key"
            raise ValueError(msg)
        return DevIdentityProvider(
            signing_key=settings.dev_signing_key.get_secret_value(),
            leeway_seconds=settings.leeway_seconds,
        )

    if settings.tenant_id is None or settings.audience is None:  # pragma: no cover - validated
        msg = "the Entra ID provider requires a tenant id and an audience"
        raise ValueError(msg)

    return EntraIdentityProvider(
        jwks_uri=settings.jwks_uri,
        tenant_id=settings.tenant_id,
        audience=settings.audience,
        jwks_cache_seconds=settings.jwks_cache_seconds,
        leeway_seconds=settings.leeway_seconds,
    )
