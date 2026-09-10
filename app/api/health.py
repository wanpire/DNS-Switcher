from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.db.session import engine

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(response: Response) -> dict:
    db_status = "ok"
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - health check must never raise
        db_status = f"error: {exc.__class__.__name__}"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {"status": "ok" if db_status == "ok" else "degraded", "db": db_status}
