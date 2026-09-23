# Changelog

Alle nennenswerten Aenderungen an `specula-client` werden hier dokumentiert.

Format angelehnt an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/), Versionierung
folgt [SemVer](https://semver.org/lang/de/) ueber Git-Tags (`vX.Y.Z`) - siehe README,
Abschnitt "Versionierung".

## [0.1.4] - 2026-09-23

### Behoben

- **Fork-Safety** (TF-916): `SpeculaLogHandler`s prozessweiter Hintergrund-Sender-Thread
  (`_get_specula_queue()`) hatte keine Fork-Awareness. Python-Threads überleben `fork()`
  nicht, aber ein bereits vor dem Fork initialisierter Queue/Thread-Singleton wird
  unverändert in Kind-Prozesse kopiert und wirkt dort gültig — betroffen insbesondere
  Celerys `prefork`-Pool: Log-Einträge aus geforkten Kind-Prozessen wurden lautlos
  verschluckt, nie versendet. `os.register_at_fork(after_in_child=...)` setzt den
  Singleton jetzt nach jedem Fork zurück und erzwingt lazy Re-Initialisierung im
  Kind-Prozess.

[0.1.4]: https://github.com/talent-factory/specula-client-python/releases/tag/v0.1.4

## [0.1.3] - 2026-09-21

### Behoben

- **Sanitizing** (TF-895 Nachbesserung): `sanitize_url()` trennt die URL-Autorität jetzt am
  **letzten** `@` vor dem ersten `/` statt am ersten — wie WHATWG-URL-Parser (Browser). Der
  `v0.1.2`-Fix redigierte bei einem Passwort mit eingebettetem `@` (z. B. `user:p@ss@host`)
  nur bis zum ersten `@`, wodurch der Rest des Passworts als scheinbarer Host im Log
  sichtbar blieb (Parser-Differential-Bug).

[0.1.3]: https://github.com/talent-factory/specula-client-python/releases/tag/v0.1.3

## [0.1.2] - 2026-09-21

### Behoben

- **Sanitizing** (TF-895): `sanitize_url()` entfernt jetzt auch Userinfo-Credentials aus der
  URL-Autorität (`https://user:pass@host/...`) — bislang wurden nur Query-String/Fragment und
  `/sign/:token`-Pfade redigiert. Kein Regressionsfehler (dieselbe Lücke bestand bereits im
  `ratum`-Original), aber ein plausibler Log-Leak-Vektor für roh vom Client geloggte URLs.

[0.1.2]: https://github.com/talent-factory/specula-client-python/releases/tag/v0.1.2

## [0.1.1] - 2026-09-21

### Hinzugefuegt

- **Sanitizing** (TF-892): `sanitize_url()`/`strip_control_chars()` fuer roh geloggten Freitext
  (URLs, Fehlermeldungen/Stacktraces) — extrahiert aus `ratum`s `routers/monitoring.py`
  (ADR-012) fuer einen zweiten Konsumenten (`examcraft-private`, TF-864). Redigiert
  Query-String/Fragment sowie `/sign/:token`-artige Pfad-Segmente in URLs bzw. neutralisiert
  Steuerzeichen (Log-Injection-Schutz).

[0.1.1]: https://github.com/talent-factory/specula-client-python/releases/tag/v0.1.1

## [0.1.0] - 2026-09-20

Erstes stabiles Release. Buendelt OTel-Traces-Setup, Logging-Integration und PII-Scrubbing,
extrahiert bzw. neu gebaut nach dem Vorbild von `ratum`-ADR-012 und `examcraft-private`s
Sentry-`EventScrubber`.

### Hinzugefuegt

- **Traces (OpenTelemetry)** (TF-849): `init_tracing()`/`instrument_fastapi_app()` fuer
  OTel-SDK-Setup inkl. optionaler FastAPI-/Celery-Instrumentierung (Extras `fastapi`/`celery`).
- **Logging** (TF-850): `SpeculaLogHandler` fuer strukturierte Log-Weiterleitung an Speculas
  OTLP/HTTP-Logs-Endpoint, inkl. Trace-Log-Korrelation und Re-Entrancy-Schutz gegen
  httpx-Feedback-Loops.
- **PII-Scrubbing** (TF-851): `PiiScrubber`/`scrub_pii()` fuer rekursives Redacting bekannter
  sensibler Feldnamen (`password`, `token`, `api_key`, `secret`, `authorization`, erweiterbar
  per `extra_denylist`) in dict/list/tuple/set/Dataclass/Namedtuple-Strukturen.

[0.1.0]: https://github.com/talent-factory/specula-client-python/releases/tag/v0.1.0
