import secrets

from fastapi import Header, HTTPException, status

from app.core.config import settings


async def require_internal_token(
    x_internal_token: str = Header(default=""),
) -> None:
    expected = settings.INTERNAL_API_TOKEN
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal API token is not configured; refusing requests.",
        )
    if not x_internal_token or not secrets.compare_digest(x_internal_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing internal API token.",
        )
