"""Port for checking that a dependency is usable."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class HealthProbe(Protocol):
    """Reports whether one external dependency is reachable and usable.

    A probe answers "can the platform serve traffic that touches this component",
    not "is the component alive". A database that accepts connections but refuses
    queries is not healthy.
    """

    @property
    def component(self) -> str:
        """Name of the component this probe covers, as it appears in reports."""
        ...

    async def check(self) -> None:
        """Verify the component.

        Returns normally when healthy.

        Raises:
            Exception: Any failure. The caller records the message and keeps
                going; one unhealthy component must not hide the others.
        """
        ...
