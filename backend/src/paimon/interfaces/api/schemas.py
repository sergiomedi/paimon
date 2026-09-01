"""Request and response models for the HTTP interface.

Deliberately separate from domain entities. A response model is a published
contract with clients; an entity is free to change with the business. Coupling
them means every domain refactor is a breaking API change.
"""

from pydantic import BaseModel, Field

from paimon.application.use_cases import ReadinessReport
from paimon.domain.entities import Principal


class LivenessResponse(BaseModel):
    """Answer to a liveness probe."""

    status: str = Field(examples=["alive"])


class ComponentStatusResponse(BaseModel):
    """State of one dependency."""

    component: str = Field(examples=["postgresql"])
    healthy: bool
    latency_ms: float
    error: str | None = None


class ReadinessResponse(BaseModel):
    """Answer to a readiness probe."""

    ready: bool
    components: list[ComponentStatusResponse]

    @classmethod
    def from_report(cls, report: ReadinessReport) -> "ReadinessResponse":
        """Build the response from the use case's report."""
        return cls(
            ready=report.is_ready,
            components=[
                ComponentStatusResponse(
                    component=component.component,
                    healthy=component.healthy,
                    latency_ms=component.latency_ms,
                    error=component.error,
                )
                for component in report.components
            ],
        )


class PrincipalResponse(BaseModel):
    """The authenticated caller."""

    subject: str
    tenant_id: str
    display_name: str | None
    roles: list[str]

    @classmethod
    def from_principal(cls, principal: Principal) -> "PrincipalResponse":
        """Build the response from the domain entity."""
        return cls(
            subject=principal.subject,
            tenant_id=principal.tenant_id,
            display_name=principal.display_name,
            roles=sorted(principal.roles),
        )


class ErrorResponse(BaseModel):
    """Uniform error body."""

    detail: str
    correlation_id: str | None = None
