import pytest
from opentelemetry import trace as trace_api
from opentelemetry.util._once import Once


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
