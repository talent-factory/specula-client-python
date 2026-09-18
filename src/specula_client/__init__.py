"""Specula-Client: Shared Observability-Client-Library fuer Talent-Factory-Produkte."""

from specula_client.tracing import init_tracing, instrument_fastapi_app

__version__ = "0.1.0"

__all__ = ["__version__", "init_tracing", "instrument_fastapi_app"]
