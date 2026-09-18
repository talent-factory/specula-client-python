"""Smoke-Test: `specula_client.tracing` gegen eine echte, minimale FastAPI-App (TF-849, AC3)."""

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.util._once import Once

from examples.minimal_fastapi_app import create_app_with_tracing


@pytest.fixture(autouse=True)
def _reset_global_tracer_provider():
    original_provider = trace_api._TRACER_PROVIDER
    trace_api._TRACER_PROVIDER_SET_ONCE = Once()
    yield
    installed_provider = trace_api._TRACER_PROVIDER
    if installed_provider is not None and installed_provider is not original_provider:
        shutdown = getattr(installed_provider, "shutdown", None)
        if callable(shutdown):
            shutdown()
    trace_api._TRACER_PROVIDER = original_provider
    trace_api._TRACER_PROVIDER_SET_ONCE = Once()


def test_app_without_tracing_config_serves_requests_normally():
    app = create_app_with_tracing(otel_exporter_endpoint="", specula_team_api_key="")

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert not isinstance(trace_api.get_tracer_provider(), TracerProvider)


def test_app_with_tracing_enabled_serves_requests_and_sets_tracer_provider():
    app = create_app_with_tracing(
        service_name="example-app",
        otel_exporter_endpoint="http://collector.internal:4318",
        specula_team_api_key="team-key",
    )

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    provider = trace_api.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    assert provider.resource.attributes["service.name"] == "example-app"
