"""SpeculaLogHandler: strukturierte Log-Weiterleitung an Speculas OTLP/HTTP-Logs-Endpoint.

Portiert aus `ratum/backend/app/logging_config.py` (ADR-012, TF-850). Hand-gerollt statt
volles OTel-Logs-SDK (``LoggerProvider``/``BatchLogRecordProcessor``): deterministisch
testbar ohne SDK-internen Batch-Thread (derselbe Kompromiss galt schon fuer den im
ratum-Original abgeloesten LogflareHandler, siehe ADR-011).

Der HTTP-Call laeuft in einem Hintergrund-Thread (ein prozessweiter Singleton-Worker ueber
eine BEGRENZTE Queue, siehe ``_get_specula_queue()``) — der aufrufende Log-Call darf durch
einen langsamen oder ausgefallenen Collector nie blockieren. Ist die Queue voll, wird der
neue Eintrag verworfen (sichtbar auf stderr) statt blockierend zu warten.

Zwei bewusste Abweichungen vom ratum-Original, beide fuer die Wiederverwendung durch
mehrere Talent-Factory-Produkte:

- ``service_name`` hat hier KEINEN Default (im ratum-Original ``"ratum-api"`` aus
  Rueckwaertskompatibilitaet mit bestehenden Aufrufern) — jeder Konsument setzt seinen
  eigenen Service-Namen explizit.
- Der Konstruktor ist keyword-only (analog zu ``specula_client.tracing``), um Verwechslungen
  zwischen den mehreren string-wertigen Parametern zu vermeiden.

Diese Bibliothek portiert bewusst nur ``SpeculaLogHandler`` selbst, nicht ratums
``configure_logging()`` (dictConfig-Wiring, ratums ``app``-Logger, ``settings``-Singleton) -
das bleibt projektspezifisch. Konsumenten haengen den Handler an ihren eigenen Logger und
setzen Level/Formatter selbst (im ratum-Original z.B. Mindestlevel WARNING, damit
routine-INFO-Logs nicht an den geteilten Collector gehen - DSGVO, siehe ratum-ADR-012).
"""

from __future__ import annotations

import logging
import queue
import sys
import threading

import httpx
from opentelemetry import trace as trace_api

# Bewusst eine BEGRENZTE Queue statt eines `ThreadPoolExecutor` mit unbegrenzter interner
# Queue: ein traeger/haengender Collector wuerde bei anhaltendem Zufluss sonst einen monoton
# wachsenden, nie begrenzten Backlog im Prozessspeicher aufbauen. Bei voller Queue wird der
# neue Log-Eintrag verworfen (nicht blockierend gewartet) - Best-effort-Zustellung bleibt
# best-effort, verschluckt aber sichtbar (stderr) statt den aufrufenden Pfad zu verzoegern.
_MAX_QUEUE_SIZE = 200
_SEND_TIMEOUT_SECONDS = 5.0

_specula_queue: queue.Queue[tuple[str, str, dict]] | None = None
_specula_worker_thread: threading.Thread | None = None
_specula_worker_lock = threading.Lock()


def _specula_worker_loop(work_queue: queue.Queue[tuple[str, str, dict]]) -> None:
    while True:
        endpoint, team_api_key, payload = work_queue.get()
        _send_to_specula(endpoint, team_api_key, payload)


def _get_specula_queue() -> queue.Queue[tuple[str, str, dict]]:
    # Lazy statt eines modulweiten Worker-Threads beim Import: ein Singleton-Worker-Thread
    # (daemon, prozessweit) statt eines neuen Threads pro SpeculaLogHandler-Instanz haelt
    # das auf genau einen Hintergrund-Thread fuer die gesamte Prozesslaufzeit begrenzt -
    # relevant, falls ein Konsument mehrere Handler-Instanzen erzeugt (z.B. bei einem
    # Logging-Reload).
    global _specula_queue, _specula_worker_thread
    if _specula_queue is None:
        with _specula_worker_lock:
            if _specula_queue is None:
                _specula_queue = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
                _specula_worker_thread = threading.Thread(
                    target=_specula_worker_loop,
                    args=(_specula_queue,),
                    name="specula-log-sender",
                    daemon=True,
                )
                _specula_worker_thread.start()
    return _specula_queue


_SEVERITY_NUMBER_BY_LEVEL = {
    logging.DEBUG: 5,
    logging.INFO: 9,
    logging.WARNING: 13,
    logging.ERROR: 17,
    logging.CRITICAL: 21,
}


def _send_to_specula(endpoint: str, team_api_key: str, payload: dict) -> None:
    # Modulfunktion statt gebundener Methode: laeuft im Singleton-Worker-Thread
    # (`_specula_worker_loop`), braucht dafuer keine Referenz auf eine bestimmte
    # Handler-Instanz - mehrere `SpeculaLogHandler`-Instanzen teilen sich denselben Worker.
    try:
        response = httpx.post(
            f"{endpoint.rstrip('/')}/v1/logs",
            headers={"Authorization": team_api_key},
            json=payload,
            timeout=_SEND_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — Netzwerk-/Collector-Fehler sind hier Alltag.
        print(f"[specula] Zustellung fehlgeschlagen: {exc}", file=sys.stderr)


class SpeculaLogHandler(logging.Handler):
    """Best-effort strukturierte Log-Weiterleitung an Speculas OTLP/HTTP-Logs-Endpoint."""

    def __init__(
        self,
        *,
        endpoint: str,
        team_api_key: str,
        service_name: str,
        deployment_environment: str = "",
        release_tag: str = "",
    ) -> None:
        super().__init__()
        self._endpoint = endpoint
        self._team_api_key = team_api_key
        self._service_name = service_name
        # `deployment.environment`/`service.version`: ohne diese Resource-Attribute lassen
        # sich Logs im geteilten Specula-Collector weder nach Umgebung trennen noch einem
        # Deploy zuordnen (analog specula_client.tracing).
        self._deployment_environment = deployment_environment
        self._release_tag = release_tag

    def emit(self, record: logging.LogRecord) -> None:
        try:
            attributes = [
                {"key": "logger", "value": {"stringValue": record.name}},
            ]
            # Jedes `extra={"specula_xyz": ...}` am Log-Call wird generisch als
            # `specula.xyz`-OTLP-Attribut exportiert. Nur der ERSTE Unterstrich wird zum
            # Punkt (`specula_signal_type` -> `specula.signal_type`).
            for key, value in vars(record).items():
                if key.startswith("specula_") and value:
                    otlp_key = key.replace("_", ".", 1)
                    attributes.append({"key": otlp_key, "value": {"stringValue": str(value)}})
            resource_attributes = [
                {"key": "service.name", "value": {"stringValue": self._service_name}},
            ]
            if self._deployment_environment:
                resource_attributes.append(
                    {
                        "key": "deployment.environment",
                        "value": {"stringValue": self._deployment_environment},
                    }
                )
            if self._release_tag:
                resource_attributes.append(
                    {"key": "service.version", "value": {"stringValue": self._release_tag}}
                )
            log_record = {
                "timeUnixNano": str(int(record.created * 1_000_000_000)),
                "severityText": record.levelname,
                # OTLP-Spec: fehlendes `severityNumber` bedeutet `SEVERITY_NUMBER_UNSPECIFIED
                # (0)` - severity-basierte Filter/Alerts griffen dann nur ueber den
                # Textvergleich von `severityText`.
                "severityNumber": _SEVERITY_NUMBER_BY_LEVEL.get(record.levelno, 0),
                "body": {"stringValue": self.format(record)},
                "attributes": attributes,
            }
            # Log<->Trace-Korrelation: ohne `traceId`/`spanId` fuehrt aus einem Log kein Weg
            # zum zugehoerigen Request-Trace. No-op ausserhalb eines aktiven Spans (z.B.
            # `init_tracing()` nicht konfiguriert) - `get_current_span()` liefert dann einen
            # `INVALID_SPAN`, dessen Kontext `is_valid=False` ist.
            span_context = trace_api.get_current_span().get_span_context()
            if span_context.is_valid:
                log_record["traceId"] = format(span_context.trace_id, "032x")
                log_record["spanId"] = format(span_context.span_id, "016x")
            payload = {
                "resourceLogs": [
                    {
                        "resource": {"attributes": resource_attributes},
                        "scopeLogs": [{"logRecords": [log_record]}],
                    }
                ]
            }
        except Exception:  # noqa: BLE001 — Formatierungsfehler duerfen den Log-Aufruf nie crashen.
            self.handleError(record)
            return
        try:
            _get_specula_queue().put_nowait((self._endpoint, self._team_api_key, payload))
        except queue.Full:
            # Bewusst verworfen statt blockierend gewartet - sichtbar auf stderr statt
            # spurlos, aber der aufrufende Pfad darf dadurch nie verzoegert werden.
            print(
                f"[specula] Export-Queue voll (>{_MAX_QUEUE_SIZE} ausstehend) — "
                "Log-Eintrag verworfen",
                file=sys.stderr,
            )
