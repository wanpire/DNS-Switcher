from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.switch import router as switch_router
from app.cloudflare.client import CloudflareClient


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.cloudflare_client = CloudflareClient()
    yield
    await app.state.cloudflare_client.aclose()


app = FastAPI(title="dns-switcher", lifespan=lifespan)

app.include_router(health_router)
app.include_router(switch_router)
