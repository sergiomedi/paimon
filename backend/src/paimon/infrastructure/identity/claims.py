"""Mapping from token claims to the domain's Principal.

Kept in one place because it is the seam between the directory's vocabulary and
the platform's. Both adapters use it, so a claim shape is interpreted identically
whether the token came from Entra ID or from the development signer.
"""

from typing import Any

from paimon.domain.entities import Principal
from paimon.domain.errors import InvalidTokenError

# Entra ID issues 'oid' as the immutable per-tenant object id. 'sub' is
# pairwise and changes between applications, so 'oid' is the stable choice.
_SUBJECT_CLAIMS = ("oid", "sub")
_TENANT_CLAIM = "tid"
_NAME_CLAIM = "name"
_ROLES_CLAIM = "roles"


def principal_from_claims(claims: dict[str, Any]) -> Principal:
    """Build a principal from a verified claim set.

    Args:
        claims: Claims from an already-verified token. This function performs no
            cryptographic verification; callers must not pass unverified input.

    Returns:
        The caller the claims identify.

    Raises:
        InvalidTokenError: A claim required to identify the caller is missing.
    """
    subject = next((str(claims[name]) for name in _SUBJECT_CLAIMS if claims.get(name)), None)
    if subject is None:
        msg = f"token carries none of the subject claims {_SUBJECT_CLAIMS}"
        raise InvalidTokenError(msg)

    tenant_id = claims.get(_TENANT_CLAIM)
    if not tenant_id:
        msg = f"token is missing the '{_TENANT_CLAIM}' claim"
        raise InvalidTokenError(msg)

    raw_roles = claims.get(_ROLES_CLAIM) or []
    roles = (
        frozenset(str(role) for role in raw_roles) if isinstance(raw_roles, list) else frozenset()
    )

    display_name = claims.get(_NAME_CLAIM)
    return Principal(
        subject=subject,
        tenant_id=str(tenant_id),
        display_name=str(display_name) if display_name else None,
        roles=roles,
    )
