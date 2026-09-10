import secrets

from fastapi import Header, HTTPException, Request, status

from app.cloudflare.client import CloudflareClient
from app.core.config import get_settings


async def verify_internal_secret(x_internal_secret: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not x_internal_secret or not secrets.compare_digest(
        x_internal_secret, settings.internal_api_shared_secret
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid internal secret"
        )


def get_cloudflare_client(request: Request) -> CloudflareClient:
    return request.app.state.cloudflare_client
