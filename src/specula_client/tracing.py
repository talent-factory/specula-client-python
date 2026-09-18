"""OpenTelemetry-Traces-Setup, extrahiert aus `ratum/backend/app/monitoring.py` (ADR-012, TF-849).

ADR-012 (ratum) legt fest: Monitoring ist fail-open (ein fehlender/ausgefallener Collector
darf den Start des Aufrufers nie verhindern) und Traces laufen ueber einen flottenweiten,
geteilten Collector (Specula). Im Original war jeder Wert (Endpoint, API-Key, Sample-Rate,
Environment, Release) an ratums `app.config.settings`-Singleton gebunden. Sampler-/Exporter-
/Instrumentierungslogik sind hier unveraendert; alle Werte werden jedoch als explizite
Parameter uebergeben, damit mehrere Talent-Factory-Produkte dieselbe Funktion wiederverwenden
koennen. Eine Abweichung: `environment`/`release` sind hier beide optional (im ratum-Original
war `deployment.environment` unconditional gesetzt) - nicht jedes Produkt fuehrt zwingend
einen Environment-String.

``instrument_fastapi_app()`` bleibt bewusst von ``init_tracing()`` getrennt: ``init_tracing()``
soll so frueh wie moeglich in jedem Prozess laufen (API- und Worker-Prozess, je mit eigenem
``service_name``), bevor ueberhaupt ein App-Objekt existiert - `instrument_fastapi_app()`
braucht dagegen die bereits konstruierte ``FastAPI``-Instanz.

``instrument_celery`` ist ein expliziter Opt-in (Default: ``False``) statt wie im
ratum-Original immer aktiv: eine generische Bibliothek soll nicht jeden Konsumenten
zwingen, `celery` + `opentelemetry-instrumentation-celery` zu installieren, nur weil
ein anderes Produkt Celery nutzt. Ebenso wird `opentelemetry-instrumentation-fastapi` in
``instrument_fastapi_app()`` lazy importiert. Ein fehlendes Extra (`ImportError`) wird dabei
bewusst separat von einem echten Instrumentierungsfehler behandelt (siehe unten) - sonst
verschleiert die generische Fehlermeldung, dass schlicht ein `pip install specula-client[...]`
fehlt.
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


def _log_tracing_disabled(
    otel_exporter_endpoint: str | None, specula_team_api_key: str | None
) -> None:
    logger.info(
        "Tracing deaktiviert: otel_exporter_endpoint=%s, specula_team_api_key=%s (beide "
        "erforderlich).",
        "gesetzt" if otel_exporter_endpoint else "fehlt",
        "gesetzt" if specula_team_api_key else "fehlt",
    )


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
    # Aufrufer sie nicht mitgibt (bewusste Abweichung vom ratum-Original, siehe Modul-
    # Docstring: dort war `deployment.environment` unconditional gesetzt).
    attributes: dict[str, str] = {"service.name": service_name}
    if environment:
        attributes["deployment.environment"] = environment
    if release:
        attributes["service.version"] = release
    resource = Resource.create(attributes)
    sampler = ParentBased(TraceIdRatioBased(sample_rate))
    provider = TracerProvider(resource=resource, sampler=sampler)
    exporter = OTLPSpanExporter(
        endpoint=f"{otel_exporter_endpoint.rstrip('/')}/v1/traces",
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
    sauber deaktiviert zu bleiben. Der No-op-Fall wird auf INFO geloggt, damit eine
    unbeabsichtigt fehlende Config nicht komplett unsichtbar bleibt.

    ``sample_rate`` (Default ``1.0``, wie im ratum-Original) ist bewusst nicht production-
    getunt - Aufrufer sollten ihn aus der eigenen Config setzen.

    Ein fehlender/ausgefallener Collector darf den Start des Aufrufers nie verhindern
    (ADR-012) — daher Try/Except um Provider-Aufbau und -Registrierung. Fehlt fuer
    ``instrument_celery=True`` das optionale `celery`-Extra, wird das separat und mit
    einer auf die fehlende Abhaengigkeit hinweisenden Meldung geloggt, statt es als
    allgemeinen Traces-Ausfall zu melden (der TracerProvider ist zu diesem Zeitpunkt
    bereits erfolgreich gesetzt).
    """
    if not (otel_exporter_endpoint and specula_team_api_key):
        _log_tracing_disabled(otel_exporter_endpoint, specula_team_api_key)
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
    except Exception:  # Monitoring darf den Start nie verhindern (ADR-012).
        logger.exception(
            "OpenTelemetry-Initialisierung fehlgeschlagen (otel_exporter_endpoint evtl. "
            "ungueltig) — Prozess startet trotzdem, aber ohne Traces."
        )
        return

    if not instrument_celery:
        return
    try:
        from opentelemetry.instrumentation.celery import CeleryInstrumentor
    except ImportError:
        logger.exception(
            "instrument_celery=True, aber 'opentelemetry-instrumentation-celery' ist nicht "
            "installiert — `pip install specula-client[celery]` nachholen. Traces sind "
            "aktiv, aber ohne Celery-Spans."
        )
        return
    try:
        CeleryInstrumentor().instrument()
    except Exception:  # analog init_tracing().
        logger.exception(
            "Celery-Instrumentierung fehlgeschlagen — Traces sind aktiv, aber ohne Celery-Spans."
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
        _log_tracing_disabled(otel_exporter_endpoint, specula_team_api_key)
        return

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        logger.exception(
            "instrument_fastapi_app() aufgerufen, aber 'opentelemetry-instrumentation-fastapi' "
            "ist nicht installiert — `pip install specula-client[fastapi]` nachholen. Kein "
            "Request-Tracing."
        )
        return
    try:
        FastAPIInstrumentor.instrument_app(app)
    except Exception:  # analog init_tracing().
        logger.exception(
            "FastAPI-Instrumentierung fehlgeschlagen — Prozess startet trotzdem, aber ohne "
            "Request-Traces."
        )
