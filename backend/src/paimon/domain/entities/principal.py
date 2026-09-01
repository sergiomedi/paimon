"""The authenticated caller, as the domain understands it."""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller.

    Deliberately not a copy of the token's claim set. The directory's claims are
    mapped into this shape at the boundary, so that authorization rules are
    written against domain concepts rather than against whatever the identity
    provider happens to emit. Swapping providers then changes one adapter, not
    every rule.

    Attributes:
        subject: Stable, provider-issued identifier for the caller.
        tenant_id: The organization the caller acts within. Present on every
            aggregate in the system; the isolation boundary depends on it.
        display_name: Human-readable name, when the provider supplies one.
        roles: Role names already mapped into domain vocabulary.
    """

    subject: str
    tenant_id: str
    display_name: str | None = None
    roles: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        """Reject a principal that cannot identify anyone.

        Raises:
            ValueError: If the subject or tenant is empty.
        """
        if not self.subject.strip():
            msg = "a principal must have a subject"
            raise ValueError(msg)
        if not self.tenant_id.strip():
            msg = "a principal must belong to a tenant"
            raise ValueError(msg)

    def has_role(self, role: str) -> bool:
        """Whether the caller holds the given role.

        Args:
            role: Role name in domain vocabulary.
        """
        return role in self.roles
