"""Smoke-Test: `specula_client.tracing` gegen eine echte, minimale FastAPI-App (TF-849, AC3).

Die TracerProvider-Isolation zwischen den Tests uebernimmt die
`reset_global_tracer_provider`-Fixture in `tests/conftest.py`.
"""

from fastapi.testclient import TestClient
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import specula_client.tracing as tracing_module
from examples.minimal_fastapi_app import create_app_with_tracing


def test_app_without_tracing_config_serves_requests_normally():
    app = create_app_with_tracing(otel_exporter_endpoint="", specula_team_api_key="")

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert not isinstance(trace_api.get_tracer_provider(), TracerProvider)


def test_app_with_tracing_enabled_serves_requests_and_sets_tracer_provider(monkeypatch):
    # Der OTLPSpanExporter versucht bei JEDEM Fehler (auch "Connection refused") mehrfach
    # mit exponentiellem Backoff erneut zu exportieren (Review-Fund: >6s beim Fixture-
    # Teardown, unabhaengig von DNS - selbst ein lokaler, garantiert verweigerter Port
    # loest diese Retry-Logik aus). Fuer einen deterministischen, schnellen Test wird der
    # echte Exporter durch einen In-Memory-Exporter ersetzt; der Smoke-Test prueft ohnehin
    # nur die Verdrahtung (Provider gesetzt, App bleibt funktionsfaehig), nicht den
    # Netzwerk-Export selbst.
    monkeypatch.setattr(tracing_module, "OTLPSpanExporter", lambda *a, **kw: InMemorySpanExporter())

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
