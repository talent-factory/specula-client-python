"""Tests fuer specula_client.tracing (TF-849, portiert aus ratum/backend/tests/test_monitoring.py).

Im Gegensatz zum ratum-Original werden alle Werte (Endpoint, API-Key, Sample-Rate,
Environment, Release) als explizite Parameter statt ueber ein projektspezifisches
`settings`-Singleton uebergeben. Die globale TracerProvider-Isolation zwischen den
Tests uebernimmt die `reset_global_tracer_provider`-Fixture in `tests/conftest.py`.
"""

import logging

from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider

from specula_client import tracing


def test_init_tracing_is_noop_without_endpoint(caplog):
    with caplog.at_level(logging.INFO, logger="specula_client.tracing"):
        tracing.init_tracing(
            service_name="example-api",
            otel_exporter_endpoint="",
            specula_team_api_key="team-key",
        )
    assert not isinstance(trace_api.get_tracer_provider(), TracerProvider)
    assert "Tracing deaktiviert" in caplog.text


def test_init_tracing_is_noop_without_team_api_key(caplog):
    # Beide Werte muessen gesetzt sein: sonst wuerden Traces mit leerem
    # Authorization-Header gegen einen Auth-erzwingenden Collector exportiert.
    with caplog.at_level(logging.INFO, logger="specula_client.tracing"):
        tracing.init_tracing(
            service_name="example-api",
            otel_exporter_endpoint="http://collector.internal:4318",
            specula_team_api_key="",
        )
    assert not isinstance(trace_api.get_tracer_provider(), TracerProvider)
    assert "Tracing deaktiviert" in caplog.text


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


def test_init_tracing_strips_trailing_slash_from_endpoint():
    tracing.init_tracing(
        service_name="example-api",
        otel_exporter_endpoint="http://collector.internal:4318/",
        specula_team_api_key="team-key",
    )

    processor = trace_api.get_tracer_provider()._active_span_processor._span_processors[0]
    assert processor.span_exporter._endpoint == "http://collector.internal:4318/v1/traces"


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
    assert not isinstance(trace_api.get_tracer_provider(), TracerProvider)


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


def test_init_tracing_with_missing_celery_extra_logs_actionable_message_and_keeps_traces(
    monkeypatch, caplog
):
    # Simuliert ein fehlendes optionales Extra (`specula-client[celery]` nicht installiert):
    # `sys.modules[name] = None` laesst jeden weiteren Import dieses Moduls mit ImportError
    # fehlschlagen, unabhaengig davon, ob es zuvor schon importiert wurde.
    monkeypatch.setitem(__import__("sys").modules, "opentelemetry.instrumentation.celery", None)

    with caplog.at_level(logging.ERROR, logger="specula_client.tracing"):
        tracing.init_tracing(
            service_name="example-worker",
            otel_exporter_endpoint="http://collector.internal:4318",
            specula_team_api_key="team-key",
            instrument_celery=True,
        )  # darf nicht raisen

    # Kernpunkt (Review-Fund): die Meldung muss auf das fehlende Extra hinweisen statt
    # faelschlicherweise einen kompletten Traces-Ausfall zu behaupten - und der bereits
    # erfolgreich gesetzte TracerProvider bleibt aktiv.
    assert "opentelemetry-instrumentation-celery" in caplog.text
    assert "specula-client[celery]" in caplog.text
    assert "OpenTelemetry-Initialisierung fehlgeschlagen" not in caplog.text
    assert isinstance(trace_api.get_tracer_provider(), TracerProvider)


def test_init_tracing_with_broken_celery_instrumentation_does_not_raise_and_keeps_traces(
    monkeypatch, caplog
):
    from opentelemetry.instrumentation.celery import CeleryInstrumentor

    def _boom(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(CeleryInstrumentor, "instrument", _boom)

    with caplog.at_level(logging.ERROR, logger="specula_client.tracing"):
        tracing.init_tracing(
            service_name="example-worker",
            otel_exporter_endpoint="http://collector.internal:4318",
            specula_team_api_key="team-key",
            instrument_celery=True,
        )  # darf nicht raisen

    assert "Celery-Instrumentierung fehlgeschlagen" in caplog.text
    assert isinstance(trace_api.get_tracer_provider(), TracerProvider)


def test_instrument_fastapi_app_is_noop_without_endpoint(monkeypatch, caplog):
    from fastapi import FastAPI
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    app = FastAPI()
    calls = []
    monkeypatch.setattr(FastAPIInstrumentor, "instrument_app", lambda *a, **kw: calls.append(kw))

    with caplog.at_level(logging.INFO, logger="specula_client.tracing"):
        tracing.instrument_fastapi_app(
            app,
            otel_exporter_endpoint="",
            specula_team_api_key="team-key",
        )

    assert calls == []
    assert "Tracing deaktiviert" in caplog.text


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


def test_instrument_fastapi_app_with_missing_fastapi_extra_logs_actionable_message(
    monkeypatch, caplog
):
    from fastapi import FastAPI

    monkeypatch.setitem(__import__("sys").modules, "opentelemetry.instrumentation.fastapi", None)
    app = FastAPI()

    with caplog.at_level(logging.ERROR, logger="specula_client.tracing"):
        tracing.instrument_fastapi_app(
            app,
            otel_exporter_endpoint="http://collector.internal:4318",
            specula_team_api_key="team-key",
        )  # darf nicht raisen

    assert "opentelemetry-instrumentation-fastapi" in caplog.text
    assert "specula-client[fastapi]" in caplog.text


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
