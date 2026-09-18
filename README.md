# specula-client

Geteilte Observability-Client-Library fuer Talent-Factory-Produkte (`ratum`,
`examcraft-private`, ...). Buendelt das OpenTelemetry-SDK-Setup, Logging-Integration
(`SpeculaLogHandler`) und PII-Scrubbing, die bisher pro Produkt dupliziert wurden
(siehe `ratum`-ADR-012).

Teil des [Specula](https://linear.app/talent-factory/project/specula)-Projekts:
ein selbst betriebenes, OpenTelemetry-natives Observability-Produkt, das Sentry,
Logflare und Logfire in den Talent-Factory-Produkten ablöst.

Dieses Repo ist bewusst eigenständig (nicht Teil des `specula`-Monorepos), da es
als versionierte Dependency von mehreren Produkt-Repos referenziert wird.

## Status

OTel-Traces-Setup (TF-849) und Logging-Integration (`SpeculaLogHandler`, TF-850) sind
verfügbar (beide extrahiert aus `ratum`-ADR-012). PII-Scrubbing folgt in einem Folge-Ticket.

## Installation

Es gibt (bewusst, siehe Scope) keine PyPI-Veröffentlichung. Bezug erfolgt per
Git-Dependency auf ein Tag:

```bash
pip install "specula-client[fastapi] @ git+https://github.com/talent-factory/specula-client-python.git@v0.1.0"
```

Oder als Abhängigkeit in `pyproject.toml`:

```toml
dependencies = [
    "specula-client[fastapi] @ git+https://github.com/talent-factory/specula-client-python.git@v0.1.0",
]
```

Die Instrumentierungs-Extras `fastapi` und `celery` sind optional — nur installieren,
was das jeweilige Produkt tatsächlich nutzt.

## Verwendung: Traces (OpenTelemetry)

```python
from specula_client import init_tracing, instrument_fastapi_app

# `settings` = die eigene Produkt-Config (z.B. pydantic-settings), kein Teil von specula-client.
# Frueh beim Prozessstart aufrufen (API- und Worker-Prozess je mit eigenem service_name),
# bevor eine evtl. vorhandene FastAPI-App gebaut wird. No-op, falls Endpoint/API-Key fehlen.
init_tracing(
    service_name="mein-produkt-api",
    otel_exporter_endpoint=settings.otel_exporter_endpoint,
    specula_team_api_key=settings.specula_team_api_key,
    sample_rate=settings.otel_traces_sample_rate,
    environment=settings.app_env,
    release=settings.release_tag,
    instrument_celery=True,  # nur falls das Produkt Celery nutzt (Extra "celery" noetig)
)

# Nachdem die FastAPI-App gebaut wurde (extra "fastapi" noetig):
instrument_fastapi_app(
    app,
    otel_exporter_endpoint=settings.otel_exporter_endpoint,
    specula_team_api_key=settings.specula_team_api_key,
)
```

Ein vollstaendiges, lauffaehiges Beispiel liegt in
[`examples/minimal_fastapi_app.py`](examples/minimal_fastapi_app.py).

## Verwendung: Logging (`SpeculaLogHandler`)

```python
import logging

from specula_client import SpeculaLogHandler

logger = logging.getLogger("mein-produkt")

if settings.otel_exporter_endpoint and settings.specula_team_api_key:
    handler = SpeculaLogHandler(
        endpoint=settings.otel_exporter_endpoint,
        team_api_key=settings.specula_team_api_key,
        service_name="mein-produkt-api",
        deployment_environment=settings.app_env,
        release_tag=settings.release_tag,
    )
    # Nur WARNING+ an den geteilten Collector (DSGVO) - Konsole/lokale Logs koennen
    # ein niedrigeres Level haben, das ist Sache der eigenen Logging-Konfiguration.
    #
    # WICHTIG: NIE an den Root-Logger auf INFO (oder tiefer) haengen, ohne das Level
    # explizit auf WARNING+ zu setzen - httpx (das der Handler intern nutzt) loggt jeden
    # Request selbst auf INFO. Der Handler filtert httpx/httpcore zwar selbst heraus
    # (Re-Entrancy-Schutz), ein zu niedriges Level exportiert aber trotzdem jede eigene
    # Anwendungs-INFO-Zeile an den Collector.
    handler.setLevel(logging.WARNING)
    logger.addHandler(handler)
```

Jede Log-Zeile, die innerhalb einer aktiven OTel-Span passiert (z.B. nach `init_tracing()` +
`instrument_fastapi_app()`), traegt automatisch `traceId`/`spanId` dieser Span - so fuehrt aus
einem Log im Specula-Backend ein direkter Weg zum zugehoerigen Request-Trace. Zusaetzliche
`extra={"specula_xyz": ...}`-Keyword-Argumente am Log-Call werden generisch als
`specula.xyz`-OTLP-Attribut exportiert (z.B. `extra={"specula_signal_type": "unhandled_exception"}`
fuer Trigger-Matching) - falsy Werte (`0`, `False`, `""`) werden dabei nicht exportiert.

**Zustellfehler und ein voller Export-Puffer werden NICHT ueber das eigene Logging-System
gemeldet** (Rekursionsgefahr), sondern nur auf stderr (`[specula] ...`) - wer eigenes
Alerting auf Specula-Zustellprobleme braucht, muss dafuer stderr/Container-Logs beobachten.

`configure_logging()`/dictConfig-Wiring, wie `ratum` es nutzt, ist bewusst NICHT Teil dieser
Bibliothek - jedes Produkt haengt `SpeculaLogHandler` an seine eigene Logging-Konfiguration.

## Versionierung

Semantische Versionierung (SemVer) über Git-Tags (`vX.Y.Z`). Konsumierende Repos
pinnen immer auf einen konkreten Tag, nie auf einen Branch, damit Updates
bewusst und reproduzierbar bleiben.

## Entwicklung

Das Projekt nutzt [uv](https://docs.astral.sh/uv/) für Dependency-Management.

```bash
uv sync
uv run ruff check .
uv run pytest
```
