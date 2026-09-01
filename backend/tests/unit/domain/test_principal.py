"""Tests for the Principal entity."""

import pytest

from paimon.domain.entities import Principal


class TestConstruction:
    def test_a_principal_carries_subject_tenant_and_roles(self) -> None:
        principal = Principal(
            subject="user-1",
            tenant_id="tenant-1",
            display_name="Ada",
            roles=frozenset({"reader"}),
        )
        assert principal.subject == "user-1"
        assert principal.tenant_id == "tenant-1"
        assert principal.has_role("reader")
        assert not principal.has_role("admin")

    def test_it_is_immutable(self) -> None:
        principal = Principal(subject="user-1", tenant_id="tenant-1")
        with pytest.raises(AttributeError):
            principal.subject = "someone-else"  # type: ignore[misc]

    @pytest.mark.parametrize("subject", ["", "   "])
    def test_an_empty_subject_is_refused(self, subject: str) -> None:
        with pytest.raises(ValueError, match="must have a subject"):
            Principal(subject=subject, tenant_id="tenant-1")

    @pytest.mark.parametrize("tenant_id", ["", "   "])
    def test_an_empty_tenant_is_refused(self, tenant_id: str) -> None:
        """Tenant is the isolation boundary; a principal without one is unsafe."""
        with pytest.raises(ValueError, match="must belong to a tenant"):
            Principal(subject="user-1", tenant_id=tenant_id)
