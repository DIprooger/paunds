from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

try:
    from .db_client import ensure_schema
    from .api_strategies import router as strategies_router
except ImportError:
    from db_client import ensure_schema
    from api_strategies import router as strategies_router


app = FastAPI(title="Paunds Parser Strategy API", version="1.0.0")


@app.on_event("startup")
def startup() -> None:
    ensure_schema()


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "paunds-parser-api"}


app.include_router(strategies_router, prefix="/api/v1")
