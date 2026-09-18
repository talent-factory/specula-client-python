"""PII-Scrubbing: rekursives Redacting bekannter sensibler Feldnamen in dict/JSON-Strukturen.

Neues Modul (TF-851, kein Vorbild in `ratum` - dort bislang nur grobes Log-Level-Gating,
siehe `specula_client.logging`). Vorbild fuer den Anforderungsumfang ist Sentrys
``EventScrubber`` (`examcraft-private/core/backend/config/sentry.py`): eine erweiterbare
Denylist bekannter sensibler Feldnamen statt eines Allowlist- oder NLP-basierten Ansatzes -
letzteres waere fuer strukturierte Log-/Event-``extra``-Felder (Formularpayloads, API-
Request-Bodies) weder praezise noch deterministisch testbar.

Feldname-Matching ist bewusst case-insensitiv UND Teilstring-basiert (``"token" in
"auth_token".lower()``) statt exaktem Key-Vergleich: ein Produkt wie `examcraft-private`
nennt sensible Felder nicht zwingend exakt ``password``/``token``/... (z.B. ``userPassword``,
``x-api-key``, ``refresh_token``). Ein False Positive (ein harmloses Feld wird redigiert)
ist hier bewusst das kleinere Risiko als ein False Negative (ein sensibles Feld bleibt
unredigiert und landet im geteilten Collector).

Redigierte Werte werden vollstaendig durch ``REDACTED`` ersetzt (nicht z.B. maskiert wie
``pa***rd``) - eine Teilmaskierung wuerde bei kurzen Werten (PINs, 4-stelligen Codes) kaum
Schutz bieten und erweckt zudem den falschen Eindruck, der Wert sei noch teilweise brauchbar.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

REDACTED = "[REDACTED]"

# Minimaldenylist aus AC1 (TF-851). Bewusst als `frozenset[str]` (nicht `list`): Ausdruck der
# Absicht "ungeordnete Menge ohne Duplikate", zusaetzlich unveraenderlich - ein Aufrufer kann
# dieses Modul-Konstante nicht versehentlich in-place mutieren und damit den Default fuer alle
# anderen `PiiScrubber`-Instanzen im selben Prozess verfaelschen.
DEFAULT_DENYLIST: frozenset[str] = frozenset(
    {"password", "token", "api_key", "secret", "authorization"}
)


def _normalize_denylist(extra_denylist: Iterable[str]) -> frozenset[str]:
    return frozenset(DEFAULT_DENYLIST | {term.lower() for term in extra_denylist})


def _is_denylisted(key: object, denylist: frozenset[str]) -> bool:
    # "-" -> "_" normalisiert Header-Style-Feldnamen (z.B. "x-api-key") auf dieselbe Form wie
    # die snake_case-Denylist-Begriffe ("api_key") - ohne das faende der Teilstring-Vergleich
    # unten keinen Treffer, obwohl beide Schreibweisen dasselbe Feld meinen.
    lowered = str(key).lower().replace("-", "_")
    return any(term in lowered for term in denylist)


def _scrub(value: object, denylist: frozenset[str]) -> object:
    if isinstance(value, Mapping):
        return {
            key: REDACTED if _is_denylisted(key, denylist) else _scrub(val, denylist)
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item, denylist) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub(item, denylist) for item in value)
    # Skalare (str, int, float, bool, None) sowie alle anderen, nicht rekursiv abgebildeten
    # Typen werden unveraendert durchgereicht - ohne umschliessendes dict/list gibt es keinen
    # Feldnamen, gegen den sich die Denylist pruefen liesse.
    return value


class PiiScrubber:
    """Redigiert bekannte sensible Feldnamen in beliebig verschachtelten dict/list-Strukturen.

    Eine Instanz buendelt eine (ggf. um ``extra_denylist`` erweiterte) Denylist fuer einen
    Aufrufer, der wiederholt gegen dieselbe Konfiguration scrubben will (z.B. `examcraft-
    private` mit produktspezifischen Zusatzfeldern) - die Denylist wird dabei einmalig beim
    Erzeugen normalisiert statt bei jedem ``scrub()``-Aufruf neu berechnet.
    """

    def __init__(self, *, extra_denylist: Iterable[str] = ()) -> None:
        self._denylist = _normalize_denylist(extra_denylist)

    def scrub(self, value: object) -> object:
        """Gibt eine neue, redigierte Kopie von ``value`` zurueck.

        ``value`` selbst (und alle enthaltenen dict/list/tuple-Strukturen) bleiben
        unveraendert - Aufrufer koennen denselben Log-``extra``-Payload also gefahrlos sowohl
        lokal (unredigiert) als auch redigiert an einen geteilten Collector weiterreichen.
        """
        return _scrub(value, self._denylist)


def scrub_pii(value: object, *, extra_denylist: Iterable[str] = ()) -> object:
    """Komfortfunktion fuer einmaliges Scrubbing ohne eigene ``PiiScrubber``-Instanz.

    Aequivalent zu ``PiiScrubber(extra_denylist=extra_denylist).scrub(value)``. Fuer
    wiederholtes Scrubbing mit derselben (erweiterten) Denylist ist eine eigene
    ``PiiScrubber``-Instanz vorzuziehen, da diese Funktion die Denylist bei jedem Aufruf neu
    normalisiert.
    """
    return PiiScrubber(extra_denylist=extra_denylist).scrub(value)
