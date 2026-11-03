from fastapi import APIRouter
from fastapi.responses import JSONResponse

from daemon_core.runtime_attestation import (
    READINESS_SCHEMA,
    build_runtime_attestation,
)

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/ready")
def ready() -> JSONResponse:
    try:
        attestation = build_runtime_attestation()
    except Exception:  # Readiness must fail closed without exposing internals.
        attestation = {
            "schema": READINESS_SCHEMA,
            "status": "degraded",
            "failures": ["runtime_attestation_failed"],
        }
    status_code = 200 if attestation["status"] == "ready" else 503
    return JSONResponse(content=attestation, status_code=status_code)
