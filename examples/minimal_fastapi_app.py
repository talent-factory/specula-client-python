"""Minimale Beispiel-FastAPI-App zur Verifikation von specula_client.tracing (TF-849).

Dient als lebender Smoke-Test dafuer, dass ``init_tracing()``/``instrument_fastapi_app()``
gegen eine echte FastAPI-App funktionieren (siehe ``tests/test_tracing_fastapi_smoke.py``).
"""

from fastapi import FastAPI

from specula_client import init_tracing, instrument_fastapi_app


def create_app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def create_app_with_tracing(
    *,
    service_name: str = "example-app",
    otel_exporter_endpoint: str | None,
    specula_team_api_key: str | None,
) -> FastAPI:
    init_tracing(
        service_name=service_name,
        otel_exporter_endpoint=otel_exporter_endpoint,
        specula_team_api_key=specula_team_api_key,
    )
    app = create_app()
    instrument_fastapi_app(
        app,
        otel_exporter_endpoint=otel_exporter_endpoint,
        specula_team_api_key=specula_team_api_key,
    )
    return app
