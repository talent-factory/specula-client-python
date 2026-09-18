import pytest
from opentelemetry import trace as trace_api
from opentelemetry.util._once import Once

import specula_client.logging as specula_logging


@pytest.fixture(autouse=True)
def reset_global_tracer_provider():
    """Test-Isolation: `init_tracing()` setzt einen echten globalen TracerProvider.

    Laut OTel-API darf `set_tracer_provider()` nur einmal pro Prozess etwas bewirken
    (`_TRACER_PROVIDER_SET_ONCE`); jeder weitere Aufruf wird sonst zum stillen No-op.
    Ein frisches `Once()`-Objekt vor jedem Test verhindert Reihenfolgeabhaengigkeit
    zwischen den Tests (siehe ratum-Original fuer die volle Herleitung dieses Musters).
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


@pytest.fixture(autouse=True)
def reset_specula_log_queue_singleton():
    """Test-Isolation fuer den prozessweiten Specula-Log-Queue/Worker-Singleton (TF-850).

    Ohne Reset wuerde der erste Test, der `_get_specula_queue()` real aufruft (statt sie zu
    mocken), den Singleton fuer die gesamte Testsession setzen - ein spaeter hinzugefuegter
    Test, der `_inline_queue()` vergisst, wuerde dann unbemerkt gegen den bereits laufenden
    Worker/dessen Queue arbeiten statt gegen einen frischen Zustand.
    """
    original_queue = specula_logging._specula_queue
    original_thread = specula_logging._specula_worker_thread
    yield
    specula_logging._specula_queue = original_queue
    specula_logging._specula_worker_thread = original_thread
