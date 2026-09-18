"""Tests fuer specula_client.tracing (TF-849, portiert aus ratum/backend/tests/test_monitoring.py).

Im Gegensatz zum ratum-Original werden alle Werte (Endpoint, API-Key, Sample-Rate,
Environment, Release) als explizite Parameter statt ueber ein projektspezifisches
`settings`-Singleton uebergeben.
"""

import logging

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.util._once import Once

from specula_client import tracing


@pytest.fixture(autouse=True)
def _reset_global_tracer_provider():
    """Test-Isolation: `init_tracing()` setzt einen echten globalen TracerProvider.

    Laut OTel-API darf `set_tracer_provider()` nur einmal pro Prozess etwas bewirken
    (`_TRACER_PROVIDER_SET_ONCE`); jeder weitere Aufruf wird sonst zum stillen No-op.
    Ein frisches `Once()`-Objekt vor jedem Test verhindert Reihenfolgeabhaengigkeit
    zwischen den Tests (siehe ratum-Original fuer die volle Herleitung).
    """
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


def test_init_tracing_is_noop_without_endpoint():
    tracing.init_tracing(
        service_name="example-api",
        otel_exporter_endpoint="",
        specula_team_api_key="team-key",
    )
    assert not isinstance(trace_api.get_tracer_provider(), TracerProvider)


def test_init_tracing_is_noop_without_team_api_key():
    # Beide Werte muessen gesetzt sein: sonst wuerden Traces mit leerem
    # Authorization-Header gegen einen Auth-erzwingenden Collector exportiert.
    tracing.init_tracing(
        service_name="example-api",
        otel_exporter_endpoint="http://collector.internal:4318",
        specula_team_api_key="",
    )
    assert not isinstance(trace_api.get_tracer_provider(), TracerProvider)


def test_init_tracing_sets_tracer_provider_with_service_name():
    tracing.init_tracing(
        service_name="example-worker",
        otel_exporter_endpoint="http://collector.internal:4318",
        specula_team_api_key="team-key",
        sample_rate=0.5,
    )

    provider = trace_api.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    assert provider.resource.attributes["service.name"] == "example-worker"


def test_init_tracing_tags_resource_with_environment_and_release():
    tracing.init_tracing(
        service_name="example-api",
        otel_exporter_endpoint="http://collector.internal:4318",
        specula_team_api_key="team-key",
        environment="production",
        release="abc123sha",
    )

    attrs = trace_api.get_tracer_provider().resource.attributes
    assert attrs["deployment.environment"] == "production"
    assert attrs["service.version"] == "abc123sha"


def test_init_tracing_omits_environment_and_release_when_unset():
    tracing.init_tracing(
        service_name="example-api",
        otel_exporter_endpoint="http://collector.internal:4318",
        specula_team_api_key="team-key",
    )

    attrs = trace_api.get_tracer_provider().resource.attributes
    assert "deployment.environment" not in attrs
    assert "service.version" not in attrs


def test_init_tracing_with_broken_endpoint_does_not_raise(monkeypatch, caplog):
    monkeypatch.setattr(
        tracing,
        "_build_tracer_provider",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with caplog.at_level(logging.ERROR, logger="specula_client.tracing"):
        tracing.init_tracing(
            service_name="example-api",
            otel_exporter_endpoint="http://collector.internal:4318",
            specula_team_api_key="team-key",
        )  # darf nicht raisen

    assert "OpenTelemetry-Initialisierung fehlgeschlagen" in caplog.text


def test_init_tracing_does_not_instrument_celery_by_default(monkeypatch):
    from opentelemetry.instrumentation.celery import CeleryInstrumentor

    calls = []
    monkeypatch.setattr(CeleryInstrumentor, "instrument", lambda self: calls.append(self))

    tracing.init_tracing(
        service_name="example-worker",
        otel_exporter_endpoint="http://collector.internal:4318",
        specula_team_api_key="team-key",
    )

    assert calls == []


def test_init_tracing_instruments_celery_when_requested(monkeypatch):
    from opentelemetry.instrumentation.celery import CeleryInstrumentor

    calls = []
    monkeypatch.setattr(CeleryInstrumentor, "instrument", lambda self: calls.append(self))

    tracing.init_tracing(
        service_name="example-worker",
        otel_exporter_endpoint="http://collector.internal:4318",
        specula_team_api_key="team-key",
        instrument_celery=True,
    )

    assert len(calls) == 1


def test_instrument_fastapi_app_is_noop_without_endpoint(monkeypatch):
    from fastapi import FastAPI
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    app = FastAPI()
    calls = []
    monkeypatch.setattr(FastAPIInstrumentor, "instrument_app", lambda *a, **kw: calls.append(kw))

    tracing.instrument_fastapi_app(
        app,
        otel_exporter_endpoint="",
        specula_team_api_key="team-key",
    )

    assert calls == []


def test_instrument_fastapi_app_is_noop_without_team_api_key(monkeypatch):
    from fastapi import FastAPI
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    app = FastAPI()
    calls = []
    monkeypatch.setattr(FastAPIInstrumentor, "instrument_app", lambda *a, **kw: calls.append(kw))

    tracing.instrument_fastapi_app(
        app,
        otel_exporter_endpoint="http://collector.internal:4318",
        specula_team_api_key="",
    )

    assert calls == []


def test_instrument_fastapi_app_instruments_without_header_capture(monkeypatch):
    from fastapi import FastAPI
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    app = FastAPI()
    calls = []
    monkeypatch.setattr(FastAPIInstrumentor, "instrument_app", lambda *a, **kw: calls.append(kw))

    tracing.instrument_fastapi_app(
        app,
        otel_exporter_endpoint="http://collector.internal:4318",
        specula_team_api_key="team-key",
    )

    assert len(calls) == 1
    # DSGVO-Schutz (siehe ratum-ADR-012): keine Header-Erfassung aktivieren.
    assert "http_capture_headers_server_request" not in calls[0]
    assert "http_capture_headers_server_response" not in calls[0]


def test_instrument_fastapi_app_with_broken_instrumentation_does_not_raise(monkeypatch, caplog):
    from fastapi import FastAPI
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    def _boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(FastAPIInstrumentor, "instrument_app", _boom)
    app = FastAPI()

    with caplog.at_level(logging.ERROR, logger="specula_client.tracing"):
        tracing.instrument_fastapi_app(
            app,
            otel_exporter_endpoint="http://collector.internal:4318",
            specula_team_api_key="team-key",
        )  # darf nicht raisen

    assert "FastAPI-Instrumentierung fehlgeschlagen" in caplog.text
