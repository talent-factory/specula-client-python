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

Aktuell nur das Repo-Skeleton (leeres, importierbares Package). Die eigentliche
OTel-/Logging-/Scrubbing-Funktionalität folgt in Folge-Tickets.

## Installation

Es gibt (bewusst, siehe Scope) keine PyPI-Veröffentlichung. Bezug erfolgt per
Git-Dependency auf ein Tag:

```bash
pip install git+https://github.com/talent-factory/specula-client-python.git@v0.1.0
```

Oder als Abhängigkeit in `pyproject.toml`:

```toml
dependencies = [
    "specula-client @ git+https://github.com/talent-factory/specula-client-python.git@v0.1.0",
]
```

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
