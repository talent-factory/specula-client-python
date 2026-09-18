"""Tests fuer specula_client.logging (TF-850, portiert aus ratum/backend/tests/test_logging.py).

Nur die `SpeculaLogHandler`-bezogenen Tests wurden portiert - `configure_logging()` ist
ratum-spezifisch (dictConfig, ratums `app`-Logger, `settings`-Singleton) und nicht Teil
dieser Bibliothek. Im Gegensatz zum ratum-Original hat `service_name` hier keinen Default
("ratum-api" dort aus Rueckwaertskompatibilitaet) - jeder Test setzt ihn explizit.
"""

import logging
import queue
import threading

import httpx
import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from specula_client import logging as specula_logging
from specula_client.logging import SpeculaLogHandler


def _inline_queue(monkeypatch):
    """Ersetzt die Hintergrund-Queue durch synchrone Ausfuehrung im Testthread."""

    class _SyncQueue:
        def put_nowait(self, item):
            endpoint, team_api_key, payload = item
            specula_logging._send_to_specula(endpoint, team_api_key, payload)

    monkeypatch.setattr(specula_logging, "_get_specula_queue", lambda: _SyncQueue())


class _FakeResponse:
    """Minimaler httpx.Response-Stand-in: 2xx per Default, `raise_for_status()` wirft bei Bedarf."""

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)


def _make_record(
    *, name: str = "app.email", level: int = logging.ERROR, msg: str = "boom"
) -> logging.LogRecord:
    return logging.LogRecord(
        name=name, level=level, pathname=__file__, lineno=1, msg=msg, args=(), exc_info=None,
    )


def test_specula_log_handler_emit_posts_otlp_json(monkeypatch):
    _inline_queue(monkeypatch)
    posted = []

    def _fake_post(url, *, headers, json, timeout):
        posted.append({"url": url, "headers": headers, "json": json})
        return _FakeResponse()

    monkeypatch.setattr(specula_logging.httpx, "post", _fake_post)

    handler = SpeculaLogHandler(
        endpoint="http://collector.internal:4318",
        team_api_key="team-key",
        service_name="example-api",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = _make_record()
    record.specula_signal_type = "unhandled_exception"
    handler.emit(record)

    assert len(posted) == 1
    call = posted[0]
    assert call["url"] == "http://collector.internal:4318/v1/logs"
    assert call["headers"] == {"Authorization": "team-key"}
    log_record = call["json"]["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert log_record["body"]["stringValue"] == "boom"
    assert log_record["severityText"] == "ERROR"
    attrs = {a["key"]: a["value"]["stringValue"] for a in log_record["attributes"]}
    assert attrs["specula.signal_type"] == "unhandled_exception"
    assert attrs["logger"] == "app.email"
    resource_attrs = {
        a["key"]: a["value"]["stringValue"]
        for a in call["json"]["resourceLogs"][0]["resource"]["attributes"]
    }
    assert resource_attrs["service.name"] == "example-api"


def test_send_to_specula_strips_trailing_slash_from_endpoint(monkeypatch):
    posted = []
    monkeypatch.setattr(
        specula_logging.httpx,
        "post",
        lambda url, **kw: (posted.append(url), _FakeResponse())[1],
    )

    specula_logging._send_to_specula("http://collector.internal:4318/", "team-key", {})

    assert posted == ["http://collector.internal:4318/v1/logs"]


def test_specula_log_handler_emit_without_extras_omits_specula_attributes(monkeypatch):
    _inline_queue(monkeypatch)
    posted = []
    monkeypatch.setattr(
        specula_logging.httpx, "post",
        lambda url, *, headers, json, timeout: (posted.append(json), _FakeResponse())[1],
    )

    handler = SpeculaLogHandler(
        endpoint="http://collector.internal:4318", team_api_key="team-key", service_name="example-api"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.emit(_make_record(level=logging.WARNING, msg="just a warning"))

    log_record = posted[0]["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    keys = {a["key"] for a in log_record["attributes"]}
    assert not any(k.startswith("specula.") for k in keys)


def test_specula_log_handler_emit_propagates_any_specula_extra_attribute(monkeypatch):
    _inline_queue(monkeypatch)
    posted = []
    monkeypatch.setattr(
        specula_logging.httpx, "post",
        lambda url, *, headers, json, timeout: (posted.append(json), _FakeResponse())[1],
    )

    handler = SpeculaLogHandler(
        endpoint="http://collector.internal:4318", team_api_key="team-key", service_name="example-api"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = _make_record(name="app.routers.monitoring", msg="frontend error")
    record.specula_signal_type = "unhandled_exception"
    record.specula_origin = "frontend"
    handler.emit(record)

    log_record = posted[0]["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    attrs = {a["key"]: a["value"]["stringValue"] for a in log_record["attributes"]}
    assert attrs["specula.signal_type"] == "unhandled_exception"
    assert attrs["specula.origin"] == "frontend"


def test_specula_log_handler_swallows_delivery_errors(monkeypatch, capsys):
    _inline_queue(monkeypatch)

    def _boom(*args, **kwargs):
        raise ConnectionError("collector unreachable")

    monkeypatch.setattr(specula_logging.httpx, "post", _boom)

    handler = SpeculaLogHandler(
        endpoint="http://collector.internal:4318", team_api_key="team-key", service_name="example-api"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.emit(_make_record())  # darf nicht raisen

    assert "collector unreachable" in capsys.readouterr().err


def test_specula_log_handler_swallows_http_error_status(monkeypatch, capsys):
    _inline_queue(monkeypatch)
    monkeypatch.setattr(specula_logging.httpx, "post", lambda *a, **kw: _FakeResponse(401))

    handler = SpeculaLogHandler(
        endpoint="http://collector.internal:4318", team_api_key="wrong", service_name="example-api"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.emit(_make_record())  # darf nicht raisen

    assert "[specula] Zustellung fehlgeschlagen" in capsys.readouterr().err


def test_specula_log_handler_emit_formatting_error_calls_handle_error(monkeypatch, capsys):
    _inline_queue(monkeypatch)
    submitted = []
    monkeypatch.setattr(
        specula_logging.httpx, "post", lambda *a, **kw: (submitted.append(1), _FakeResponse())[1]
    )

    handler = SpeculaLogHandler(
        endpoint="http://collector.internal:4318", team_api_key="team-key", service_name="example-api"
    )

    class _BrokenFormatter:
        def format(self, record):
            raise ValueError("boom formatting")

    handler.setFormatter(_BrokenFormatter())
    handler.emit(_make_record())  # darf nicht raisen

    assert submitted == []
    assert "Traceback" in capsys.readouterr().err


def test_get_specula_queue_returns_same_instance_across_calls():
    first = specula_logging._get_specula_queue()
    second = specula_logging._get_specula_queue()
    assert first is second


def test_get_specula_queue_has_bounded_maxsize():
    assert specula_logging._get_specula_queue().maxsize == specula_logging._MAX_QUEUE_SIZE


def test_specula_log_handler_drops_when_queue_is_full(monkeypatch, capsys):
    class _FullQueue:
        def put_nowait(self, item):
            raise queue.Full

    monkeypatch.setattr(specula_logging, "_get_specula_queue", lambda: _FullQueue())

    handler = SpeculaLogHandler(
        endpoint="http://collector.internal:4318", team_api_key="team-key", service_name="example-api"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.emit(_make_record())  # darf nicht blockieren/raisen

    assert "Export-Queue voll" in capsys.readouterr().err


def test_specula_worker_loop_processes_queued_items(monkeypatch):
    delivered = threading.Event()
    posted = []

    def _fake_post(url, *, headers, json, timeout):
        posted.append(json)
        delivered.set()
        return _FakeResponse()

    monkeypatch.setattr(specula_logging.httpx, "post", _fake_post)
    work_queue: queue.Queue[tuple[str, str, dict]] = queue.Queue(maxsize=1)
    worker = threading.Thread(
        target=specula_logging._specula_worker_loop, args=(work_queue,), daemon=True
    )
    worker.start()
    work_queue.put_nowait(("http://collector.internal:4318", "team-key", {"marker": "hi"}))

    assert delivered.wait(timeout=2.0), "Worker hat das Item nicht innert 2s verarbeitet"
    assert posted == [{"marker": "hi"}]


def test_specula_log_handler_emit_includes_deployment_environment_and_release(monkeypatch):
    _inline_queue(monkeypatch)
    posted = []
    monkeypatch.setattr(
        specula_logging.httpx, "post",
        lambda url, *, headers, json, timeout: (posted.append(json), _FakeResponse())[1],
    )

    handler = SpeculaLogHandler(
        endpoint="http://collector.internal:4318",
        team_api_key="team-key",
        service_name="example-api",
        deployment_environment="production",
        release_tag="abc123sha",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.emit(_make_record())

    resource_attrs = {
        a["key"]: a["value"]["stringValue"]
        for a in posted[0]["resourceLogs"][0]["resource"]["attributes"]
    }
    assert resource_attrs["deployment.environment"] == "production"
    assert resource_attrs["service.version"] == "abc123sha"


def test_specula_log_handler_emit_omits_environment_and_release_when_unset(monkeypatch):
    _inline_queue(monkeypatch)
    posted = []
    monkeypatch.setattr(
        specula_logging.httpx, "post",
        lambda url, *, headers, json, timeout: (posted.append(json), _FakeResponse())[1],
    )

    handler = SpeculaLogHandler(
        endpoint="http://collector.internal:4318", team_api_key="team-key", service_name="example-api"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.emit(_make_record())

    resource_attrs = {a["key"] for a in posted[0]["resourceLogs"][0]["resource"]["attributes"]}
    assert "deployment.environment" not in resource_attrs
    assert "service.version" not in resource_attrs


@pytest.mark.parametrize(
    ("level", "expected_number"),
    [
        (logging.WARNING, 13),
        (logging.ERROR, 17),
        (logging.CRITICAL, 21),
    ],
)
def test_specula_log_handler_emit_maps_severity_number(monkeypatch, level, expected_number):
    _inline_queue(monkeypatch)
    posted = []
    monkeypatch.setattr(
        specula_logging.httpx, "post",
        lambda url, *, headers, json, timeout: (posted.append(json), _FakeResponse())[1],
    )

    handler = SpeculaLogHandler(
        endpoint="http://collector.internal:4318", team_api_key="team-key", service_name="example-api"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.emit(_make_record(level=level))

    log_record = posted[0]["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert log_record["severityNumber"] == expected_number


def test_specula_log_handler_emit_omits_trace_ids_outside_a_span(monkeypatch):
    _inline_queue(monkeypatch)
    posted = []
    monkeypatch.setattr(
        specula_logging.httpx, "post",
        lambda url, *, headers, json, timeout: (posted.append(json), _FakeResponse())[1],
    )

    handler = SpeculaLogHandler(
        endpoint="http://collector.internal:4318", team_api_key="team-key", service_name="example-api"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.emit(_make_record())

    log_record = posted[0]["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert "traceId" not in log_record
    assert "spanId" not in log_record


def test_specula_log_handler_emit_includes_trace_and_span_id_inside_a_span(monkeypatch):
    # AC3 (TF-850): Trace<->Log-Korrelation nachweislich funktionsfaehig.
    _inline_queue(monkeypatch)
    posted = []
    monkeypatch.setattr(
        specula_logging.httpx, "post",
        lambda url, *, headers, json, timeout: (posted.append(json), _FakeResponse())[1],
    )

    handler = SpeculaLogHandler(
        endpoint="http://collector.internal:4318", team_api_key="team-key", service_name="example-api"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = _make_record()

    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("test-span") as span:
        handler.emit(record)
        expected_trace_id = format(span.get_span_context().trace_id, "032x")
        expected_span_id = format(span.get_span_context().span_id, "016x")

    log_record = posted[0]["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert log_record["traceId"] == expected_trace_id
    assert log_record["spanId"] == expected_span_id
    provider.shutdown()
