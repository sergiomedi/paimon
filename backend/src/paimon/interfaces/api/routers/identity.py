"""Endpoints describing the authenticated caller."""

from fastapi import APIRouter, status

from paimon.interfaces.api.dependencies import CurrentPrincipal
from paimon.interfaces.api.schemas import ErrorResponse, PrincipalResponse

router = APIRouter(tags=["identity"])


@router.get(
    "/me",
    response_model=PrincipalResponse,
    summary="The authenticated caller",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    },
)
async def me(principal: CurrentPrincipal) -> PrincipalResponse:
    """Return the caller as the platform understands them.

    Thin by design. Its value is that it exercises the whole authentication path
    — header, adapter, claim mapping, domain entity — so a wiring mistake shows
    up here rather than in the first feature that depends on it.
    """
    return PrincipalResponse.from_principal(principal)
