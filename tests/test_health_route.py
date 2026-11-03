from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.routes import health as health_route


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(health_route.router)
    return TestClient(app)


def test_health_remains_backward_compatible():
    response = _client().get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_returns_200_for_ready_runtime(monkeypatch):
    monkeypatch.setattr(
        health_route,
        "build_runtime_attestation",
        lambda: {"schema": "test", "status": "ready"},
    )

    response = _client().get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_ready_returns_503_for_degraded_runtime(monkeypatch):
    monkeypatch.setattr(
        health_route,
        "build_runtime_attestation",
        lambda: {"schema": "test", "status": "degraded"},
    )

    response = _client().get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


def test_ready_fails_closed_without_exposing_internal_errors(monkeypatch):
    def fail() -> dict:
        raise RuntimeError("private path and secret")

    monkeypatch.setattr(health_route, "build_runtime_attestation", fail)

    response = _client().get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "schema": "daemon.runtime-readiness.v1",
        "status": "degraded",
        "failures": ["runtime_attestation_failed"],
    }
    assert "private" not in response.text
    assert "secret" not in response.text
