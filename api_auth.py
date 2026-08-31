from __future__ import annotations

import os

from fastapi import Header, HTTPException, status


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> str:
    expected = os.getenv("PARSER_API_KEY", "").strip()

    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PARSER_API_KEY is not configured",
        )

    if not x_api_key or x_api_key.strip() != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    return x_api_key
