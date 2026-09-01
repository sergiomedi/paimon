"""Liveness and readiness endpoints."""

from fastapi import APIRouter, Response, status

from paimon.interfaces.api.dependencies import CheckReadinessDep
from paimon.interfaces.api.schemas import LivenessResponse, ReadinessResponse

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=LivenessResponse, summary="Liveness probe")
async def live() -> LivenessResponse:
    """Report that the process is running.

    Touches no dependency on purpose. A liveness probe that fails because the
    database is down gets the container killed and restarted, which does not fix
    the database and does destroy in-flight work.
    """
    return LivenessResponse(status="alive")


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def ready(check_readiness: CheckReadinessDep, response: Response) -> ReadinessResponse:
    """Report whether this instance can serve traffic.

    Returns 503 when any dependency is unusable, so the load balancer stops
    sending traffic here while the process stays up. The body lists every
    component either way: knowing *which* dependency is down is the difference
    between a diagnosis and a restart.
    """
    report = await check_readiness()
    if not report.is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse.from_report(report)
