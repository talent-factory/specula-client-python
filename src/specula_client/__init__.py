"""Specula-Client: Shared Observability-Client-Library fuer Talent-Factory-Produkte."""

from specula_client.logging import SpeculaLogHandler
from specula_client.sanitizing import sanitize_url, strip_control_chars
from specula_client.scrubbing import DEFAULT_DENYLIST, REDACTED, PiiScrubber, scrub_pii
from specula_client.tracing import init_tracing, instrument_fastapi_app

__version__ = "0.1.1"

__all__ = [
    "DEFAULT_DENYLIST",
    "REDACTED",
    "PiiScrubber",
    "SpeculaLogHandler",
    "__version__",
    "init_tracing",
    "instrument_fastapi_app",
    "sanitize_url",
    "scrub_pii",
    "strip_control_chars",
]
