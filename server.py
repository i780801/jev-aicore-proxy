"""
FastAPI proxy: exposes TypeSafe Jev 1.13 (via OpenRouter) behind an
SAP AI Core managed inference endpoint.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SDK client — initialised once at startup
# ---------------------------------------------------------------------------

_jev_client: Any = None


def _init_client() -> Any:
    """Construct a TypeSafeClient from the injected secret.  Raises RuntimeError
    if OPENROUTER_API_KEY is missing so the container fails fast at startup."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY environment variable is not set or is empty."
        )

    try:
        from typesafe_sdk import TypeSafeClient  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "typesafe-sdk is not installed; add it to requirements.txt."
        ) from exc

    return TypeSafeClient(
        api_key=api_key,
        base_url="https://openrouter.ai/api",
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _jev_client
    _jev_client = _init_client()
    log.info("TypeSafeClient initialised successfully.")
    yield
    _jev_client = None


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="jev-aicore-proxy",
    version="1.0.0",
    docs_url=None,   # disable interactive docs in a deployed container
    redoc_url=None,
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

MAX_BODY_BYTES = 64 * 1024  # 64 KB — sufficient for any realistic decision payload


class SystemOneRequest(BaseModel):
    state: dict[str, Any] = Field(..., description="Decision context.")
    questions: dict[str, Any] = Field(..., description="Structured question definitions.")


# ---------------------------------------------------------------------------
# Request-size guard (applied before Pydantic validation)
# ---------------------------------------------------------------------------

@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_BODY_BYTES:
        return JSONResponse(
            status_code=413,
            content={"detail": "Request body too large."},
        )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/v2/health")
def health():
    return {"status": "ok"}


@app.post("/v1/systemone")
def system_one(request: SystemOneRequest):
    """Proxy a System One inference request to Jev 1.13 via OpenRouter."""
    if _jev_client is None:
        raise HTTPException(
            status_code=503,
            detail="Inference client is not available.",
        )

    try:
        result = _jev_client.system_one(
            model="jev-1.13",
            state=request.state,
            questions=request.questions,
        )
    except Exception as exc:
        # Log the full error internally; return a safe message externally.
        log.error("Upstream inference error: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=502,
            detail="Upstream inference request failed.",
        ) from exc

    # Serialise the SDK response.  answers values are Pydantic models.
    answers: dict[str, Any] = {}
    for key, answer in result.answers.items():
        answers[key] = answer.model_dump() if hasattr(answer, "model_dump") else dict(answer)

    usage: dict[str, Any] | None = None
    if result.usage is not None:
        usage = result.usage.model_dump() if hasattr(result.usage, "model_dump") else dict(result.usage)

    return {
        "model": result.model,
        "usage": usage,
        "answers": answers,
    }
