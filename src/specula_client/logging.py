"""SpeculaLogHandler: strukturierte Log-Weiterleitung an Speculas OTLP/HTTP-Logs-Endpoint.

Portiert aus `ratum/backend/app/logging_config.py` (ADR-012, TF-850). Hand-gerollt statt
volles OTel-Logs-SDK (``LoggerProvider``/``BatchLogRecordProcessor``): deterministisch
testbar ohne SDK-internen Batch-Thread (derselbe Kompromiss galt schon fuer den im
ratum-Original abgeloesten LogflareHandler).

Der HTTP-Call laeuft in einem Hintergrund-Thread (ein prozessweiter Singleton-Worker ueber
eine BEGRENZTE Queue, siehe ``_get_specula_queue()``) — der aufrufende Log-Call darf durch
einen langsamen oder ausgefallenen Collector nie blockieren. Ist die Queue voll, wird der
neue Eintrag verworfen (sichtbar auf stderr) statt blockierend zu warten.

Re-Entrancy-Schutz (Review-Fund, TF-850): ``httpx`` loggt jeden Request selbst ueber die
Logger ``httpx``/``httpcore`` auf INFO. Haengt ein Konsument diesen Handler an einen Logger,
der solche Records empfaengt (z.B. den Root-Logger auf INFO - ein verbreitetes Setup),
entstuende ohne Schutz eine sich selbst verstaerkende Schleife Log -> POST -> httpx-eigenes
INFO-Log -> POST -> ... ``emit()`` verwirft daher (a) Records aus dem eigenen Worker-Thread
und (b) Records von ``httpx``/``httpcore`` selbst, bevor ueberhaupt ein Payload gebaut wird.

Zwei bewusste Abweichungen vom ratum-Original, beide fuer die Wiederverwendung durch
mehrere Talent-Factory-Produkte:

- ``service_name`` hat hier KEINEN Default (im ratum-Original ``"ratum-api"`` aus
  Rueckwaertskompatibilitaet mit bestehenden Aufrufern) — jeder Konsument setzt seinen
  eigenen Service-Namen explizit.
- Der Konstruktor ist keyword-only (analog zu ``specula_client.tracing``) und validiert
  ``endpoint``/``team_api_key`` sofort: ratums ``configure_logging()`` pruefte frueher
  "beide Werte gesetzt, sonst kein Handler" ausserhalb des Handlers selbst - da diese
  Bibliothek ``configure_logging()`` bewusst nicht mitportiert (s.u.), uebernimmt der
  Konstruktor diese Pruefung, statt eine fehlende Konfiguration erst zur Laufzeit als
  generischen "Zustellung fehlgeschlagen"-Fehler zu tarnen.

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
import time
import traceback

import httpx
from opentelemetry import trace as trace_api

# Bewusst eine BEGRENZTE Queue statt eines `ThreadPoolExecutor` mit unbegrenzter interner
# Queue: ein traeger/haengender Collector wuerde bei anhaltendem Zufluss sonst einen monoton
# wachsenden, nie begrenzten Backlog im Prozessspeicher aufbauen. Bei voller Queue wird der
# neue Log-Eintrag verworfen (nicht blockierend gewartet) - Best-effort-Zustellung bleibt
# best-effort, verschluckt aber sichtbar (stderr) statt den aufrufenden Pfad zu verzoegern.
_MAX_QUEUE_SIZE = 200
_SEND_TIMEOUT_SECONDS = 5.0
# Obergrenze fuer flush(): ein haengender Collector darf den Prozess-Shutdown nicht
# blockieren (dasselbe Fail-open-Prinzip wie ADR-012).
_FLUSH_TIMEOUT_SECONDS = 2.0

# Logger, deren eigene Records nie an Specula weitergeleitet werden (Re-Entrancy-Schutz,
# siehe Modul-Docstring) - das waere ohnehin nur Transport-Rauschen, kein Anwendungslog.
_SILENCED_LOGGER_PREFIXES = ("httpx", "httpcore")

_specula_queue: queue.Queue[tuple[str, str, dict]] | None = None
_specula_worker_thread: threading.Thread | None = None
_specula_worker_lock = threading.Lock()


def _specula_worker_loop(work_queue: queue.Queue[tuple[str, str, dict]]) -> None:
    while True:
        endpoint, team_api_key, payload = work_queue.get()
        try:
            _send_to_specula(endpoint, team_api_key, payload)
        except Exception:  # noqa: BLE001 — der Worker-Thread darf NIE sterben (Review-Fund):
            # `_send_to_specula()` faengt bereits alle regulaeren Zustellfehler selbst; dieser
            # aeussere Schutzring deckt nur den unerwarteten Rest ab (z.B. wenn `print()`
            # selbst fehlschlaegt). Ohne ihn wuerde die Queue lautlos volllaufen und jeder
            # weitere Log-Eintrag verworfen, ohne dass sichtbar wuerde, dass der Grund ein
            # toter Worker statt ein langsamer Collector ist.
            traceback.print_exc(file=sys.stderr)
        finally:
            # `task_done()` erlaubt `flush()`, ueber `unfinished_tasks` zu erkennen, wann ein
            # Item nicht nur aus der Queue entnommen, sondern auch fertig verarbeitet wurde -
            # `work_queue.empty()` allein waere bereits direkt nach `get()` wahr, obwohl der
            # Versand selbst noch laeuft.
            work_queue.task_done()


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
                # Queue/Thread erst den Globals zuweisen, NACHDEM `start()` erfolgreich war
                # (Review-Fund): wuerde `start()` selbst fehlschlagen (z.B. OS-Thread-Limit),
                # bliebe sonst eine Queue ohne Konsumenten als Singleton stehen - jeder
                # weitere Log-Eintrag wuerde dann verworfen, ohne dass ein neuer Versuch
                # jemals unternommen wird.
                new_queue: queue.Queue[tuple[str, str, dict]] = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
                worker = threading.Thread(
                    target=_specula_worker_loop,
                    args=(new_queue,),
                    name="specula-log-sender",
                    daemon=True,
                )
                worker.start()
                _specula_queue = new_queue
                _specula_worker_thread = worker
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
        print(
            f"[specula] Zustellung fehlgeschlagen: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


class SpeculaLogHandler(logging.Handler):
    """Best-effort strukturierte Log-Weiterleitung an Speculas OTLP/HTTP-Logs-Endpoint.

    Zustellfehler (Netzwerk/Collector) und ein voller Export-Puffer werden im Hintergrund-
    Thread verschluckt, aber auf stderr notiert (``[specula] ...``) statt spurlos zu
    verschwinden - bewusst NICHT ueber das eigene ``logging``-Modul (Rekursionsgefahr, siehe
    Modul-Docstring zum Re-Entrancy-Schutz). Wer eigenes Alerting auf Specula-
    Zustellprobleme braucht, muss dafuer stderr/Container-Logs beobachten.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        team_api_key: str,
        service_name: str,
        deployment_environment: str = "",
        release_tag: str = "",
    ) -> None:
        # Fail-fast statt eines spaeter, bei jedem emit(), als generischer "Zustellung
        # fehlgeschlagen"-Fehler getarnten Konfigurationsfehlers (Review-Fund) - siehe
        # Modul-Docstring.
        if not endpoint:
            raise ValueError("SpeculaLogHandler: 'endpoint' darf nicht leer sein.")
        if not team_api_key:
            raise ValueError("SpeculaLogHandler: 'team_api_key' darf nicht leer sein.")
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
        # Re-Entrancy-Schutz (siehe Modul-Docstring): weder eigene Worker-Thread-Aktivitaet
        # noch httpx/httpcore-eigene Transport-Logs duerfen erneut eingereiht werden.
        if threading.current_thread() is _specula_worker_thread:
            return
        if record.name.partition(".")[0] in _SILENCED_LOGGER_PREFIXES:
            return
        try:
            attributes = [
                {"key": "logger", "value": {"stringValue": record.name}},
            ]
            # Jedes `extra={"specula_xyz": ...}` am Log-Call wird generisch als
            # `specula.xyz`-OTLP-Attribut exportiert. Nur der ERSTE Unterstrich wird zum
            # Punkt (`specula_signal_type` -> `specula.signal_type`). Falsy Werte (0, False,
            # "") werden NICHT exportiert (`and value`, unveraendert vom ratum-Original) -
            # fuer eine "nicht gesetzt" vs. "0/False/leer"-Unterscheidung muesste der
            # Aufrufer den Wert explizit als nicht-falsy uebergeben (z.B. `str(value)`).
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

    def flush(self) -> None:
        """Wartet bounded, bis die Export-Queue leer ist.

        Ohne Override haben ``logging.shutdown()``/``Handler.close()`` keine Wirkung auf den
        Daemon-Worker-Thread (Review-Fund): beim Prozessende (inkl. Crash) gingen bis zu
        ``_MAX_QUEUE_SIZE`` bereits eingereihte Records sonst spurlos verloren - ausgerechnet
        im Moment, in dem Zustellung am wichtigsten waere. Bounded statt unbegrenzt: ein
        haengender Collector darf den Prozess-Shutdown nicht blockieren.

        Ruft bewusst NICHT `_get_specula_queue()` auf: das wuerde bei einem Handler, der nie
        `emit()`-t hat (z.B. weil sein Level nie erreicht wurde), unnoetig einen Worker-
        Thread starten, nur um ihn sofort wieder ungenutzt zu lassen.
        """
        if _specula_queue is not None:
            # `unfinished_tasks` statt `empty()`: die Queue gilt schon direkt nach `get()`
            # als leer, obwohl der Versand im Worker-Thread noch laeuft - erst `task_done()`
            # (siehe `_specula_worker_loop`) senkt `unfinished_tasks` auf 0.
            deadline = time.monotonic() + _FLUSH_TIMEOUT_SECONDS
            while _specula_queue.unfinished_tasks > 0 and time.monotonic() < deadline:
                time.sleep(0.05)
        super().flush()
