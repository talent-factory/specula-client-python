"""OpenTelemetry-Traces-Setup, extrahiert aus `ratum/backend/app/monitoring.py` (ADR-012, TF-849).

Im Original war jeder Wert (Endpoint, API-Key, Sample-Rate, Environment, Release) an
ratums `app.config.settings`-Singleton gebunden. Hier werden dieselbe Sampler-/
Exporter-/Instrumentierungslogik beibehalten, aber alle Werte als explizite Parameter
uebergeben, damit mehrere Talent-Factory-Produkte dieselbe Funktion wiederverwenden
koennen.

``instrument_fastapi_app()`` bleibt bewusst von ``init_tracing()`` getrennt: sie
braucht eine bereits konstruierte ``FastAPI``-Instanz, die beim allgemeinen Init
(vor dem App-Aufbau, damit auch z.B. Celery-seitige Fehler erfasst wuerden) noch
nicht existiert (siehe ratum-Original fuer die volle Herleitung).

``instrument_celery`` ist ein expliziter Opt-in (Default: ``False``) statt wie im
ratum-Original immer aktiv: eine generische Bibliothek soll nicht jeden Konsumenten
zwingen, `celery` + `opentelemetry-instrumentation-celery` zu installieren, nur weil
ein anderes Produkt Celery nutzt. Der Import passiert daher lazy, ebenso wie der von
`opentelemetry-instrumentation-fastapi` in ``instrument_fastapi_app()``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger("specula_client.tracing")


def _build_tracer_provider(
    service_name: str,
    *,
    otel_exporter_endpoint: str,
    specula_team_api_key: str,
    sample_rate: float,
    environment: str | None,
    release: str | None,
) -> TracerProvider:
    # `deployment.environment`/`service.version`: Specula ist ein flottenweiter,
    # geteilter Collector — ohne `deployment.environment` liessen sich Traces aus
    # staging/production nicht trennen, ohne `service.version` (i.d.R. Git-SHA) kein
    # Incident->Deploy-Mapping mehr. Weggelassen statt eines leeren Werts, wenn der
    # Aufrufer sie nicht mitgibt.
    attributes: dict[str, str] = {"service.name": service_name}
    if environment:
        attributes["deployment.environment"] = environment
    if release:
        attributes["service.version"] = release
    resource = Resource.create(attributes)
    sampler = ParentBased(TraceIdRatioBased(sample_rate))
    provider = TracerProvider(resource=resource, sampler=sampler)
    exporter = OTLPSpanExporter(
        endpoint=f"{otel_exporter_endpoint}/v1/traces",
        headers={"Authorization": specula_team_api_key},
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def init_tracing(
    *,
    service_name: str,
    otel_exporter_endpoint: str | None,
    specula_team_api_key: str | None,
    sample_rate: float = 1.0,
    environment: str | None = None,
    release: str | None = None,
    instrument_celery: bool = False,
) -> None:
    """Initialisiert Traces (+ optional Celery-Instrumentierung). Sonst no-op.

    Beide Werte (``otel_exporter_endpoint``, ``specula_team_api_key``) muessen gesetzt
    sein: fehlt nur der Key, wuerden sonst Traces mit leerem ``Authorization``-Header
    gegen einen Auth-erzwingenden Collector exportiert (Hintergrund-Retry-Loop) statt
    sauber deaktiviert zu bleiben (siehe ratum-Original, "I2, finaler Review-Fund").

    Ein fehlender/ausgefallener Collector darf den Start des Aufrufers nie verhindern
    (ADR-012) — daher Try/Except um die komplette Initialisierung.
    """
    if not (otel_exporter_endpoint and specula_team_api_key):
        return
    try:
        provider = _build_tracer_provider(
            service_name,
            otel_exporter_endpoint=otel_exporter_endpoint,
            specula_team_api_key=specula_team_api_key,
            sample_rate=sample_rate,
            environment=environment,
            release=release,
        )
        trace.set_tracer_provider(provider)
        if instrument_celery:
            from opentelemetry.instrumentation.celery import CeleryInstrumentor

            CeleryInstrumentor().instrument()
    except Exception:  # Monitoring darf den Start nie verhindern (ADR-012).
        logger.exception(
            "OpenTelemetry-Initialisierung fehlgeschlagen (otel_exporter_endpoint evtl. "
            "ungueltig) — Prozess startet trotzdem, aber ohne Traces."
        )


def instrument_fastapi_app(
    app: FastAPI,
    *,
    otel_exporter_endpoint: str | None,
    specula_team_api_key: str | None,
) -> None:
    """Instrumentiert die FastAPI-App fuer Request-Traces. Sonst no-op.

    KEINE ``http_capture_headers_*``-Optionen gesetzt (OTel-Default: keine Header-
    Erfassung) — das ist hier bewusst der DSGVO-Schutz (siehe ratum-ADR-012).
    """
    if not (otel_exporter_endpoint and specula_team_api_key):
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception:  # analog init_tracing().
        logger.exception(
            "FastAPI-Instrumentierung fehlgeschlagen — Prozess startet trotzdem, aber ohne "
            "Request-Traces."
        )
