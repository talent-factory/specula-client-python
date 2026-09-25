"""PII-Scrubbing: rekursives Redacting bekannter sensibler Feldnamen in dict/JSON-Strukturen.

Neues Modul (TF-851, in `ratum` bislang nur punktuelle URL-/Control-Char-Sanitisierung in
`monitoring.py` und grobes Log-Level-Gating - kein generischer, feldnamen-basierter Scrubber).
Vorbild fuer den Anforderungsumfang ist Sentrys ``EventScrubber``
(`examcraft-private/core/backend/config/sentry.py`): eine erweiterbare Denylist bekannter
sensibler Feldnamen statt eines Allowlist- oder NLP-basierten Ansatzes - letzteres waere fuer
strukturierte Log-/Event-``extra``-Felder (Formularpayloads, API-Request-Bodies) weder
praezise noch deterministisch testbar. E-Mail-Adressen sind bewusst NICHT Teil der
Default-Denylist (anders als in der Linear-Ticket-Beschreibung erwaehnt) - eine E-Mail ist im
Gegensatz zu Passwoertern/Tokens haeufig selbst fachlich relevant (z.B. Nutzerzuordnung in
einem Log); wer sie dennoch redigieren will, nutzt ``extra_denylist=["email"]``.

Feldname-Matching ist bewusst case-insensitiv UND Teilstring-basiert (``"token" in
"auth_token"``) statt exaktem Key-Vergleich: ein Produkt wie `examcraft-private` nennt
sensible Felder nicht zwingend exakt ``password``/``token``/... (z.B. ``userPassword``,
``refresh_token``). Ein False Positive (ein harmloses Feld wird redigiert, z.B. `total_tokens`
bei einem LLM-Produkt) ist hier bewusst das kleinere Risiko als ein False Negative (ein
sensibles Feld bleibt unredigiert und landet im geteilten Collector) - wer das vermeiden will,
kann ueber einen praeziseren, produktspezifischen Denylist-Begriff selbst gegensteuern.

Sowohl Feldnamen als auch Denylist-Begriffe werden vor dem Vergleich von ``_``/``-``
befreit (``"x-api-key"``/``"apiKey"``/``"api_key"`` werden alle auf ``"apikey"`` abgebildet) -
ohne das faende z.B. `apiKey` (camelCase, typisch fuer JS-Frontend-Payloads) oder ein
per ``extra_denylist`` uebergebener Bindestrich-Begriff nie einen Treffer.

Redigierte Werte werden vollstaendig durch den Platzhalter ``REDACTED`` ersetzt (Wert
``"[REDACTED]"``, nicht z.B. maskiert wie ``pa***rd``) - eine Teilmaskierung wuerde bei kurzen
Werten (PINs, 4-stelligen Codes) kaum Schutz bieten und erweckt zudem den falschen Eindruck,
der Wert sei noch teilweise brauchbar.

Unterstuetzt werden dict/list/tuple/set/``frozenset``/Dataclasses/Namedtuples sowie JSON-taugliche
Skalare (str/int/float/bool/bytes/None), jeweils beliebig verschachtelt - fuer Dataclasses und
Namedtuples werden Feldnamen genauso gegen die Denylist geprueft wie dict-Keys. Ein nicht
erkannter Objekttyp (z.B. ein Pydantic-Modell oder eine beliebige eigene Klasse) wird NICHT
stillschweigend unredigiert durchgereicht (Review-Fund, TF-851: das waere ein stiller
PII-Leak, da ein solches Objekt genauso sensible Felder als Attribute tragen kann wie ein
dict) - stattdessen wirft ``scrub()``/``scrub_pii()`` einen ``TypeError``, der den Aufrufer
zwingt, vorher explizit zu serialisieren (z.B. ``.model_dump()``/``dataclasses.asdict()``).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping

REDACTED = "[REDACTED]"

# Einzige Quelle der Wahrheit fuer die "specula_"-Extra-Konvention (TF-937): sowohl
# `SpeculaLogHandler.emit()` (specula_client.logging, wandelt `specula_xyz` in ein
# `specula.xyz`-OTLP-Attribut) als auch produktspezifische Scrubbing-Filter (z.B. ratums
# `app.logging_config.PiiScrubbingLogFilter`) muessen denselben Praefix verwenden - vorher war
# er in beiden, separat versionierten Paketen hartkodiert dupliziert, sodass ein kuenftiger
# Library-Bump mit geaenderter Konvention den Scrubbing-Filter stillschweigend wirkungslos
# gemacht haette (PII-Leak statt sichtbarem Fehler).
SPECULA_EXTRA_PREFIX = "specula_"

# Minimaldenylist aus AC1 (TF-851). Bewusst als `frozenset[str]` (nicht `list`): Ausdruck der
# Absicht "ungeordnete Menge ohne Duplikate", zusaetzlich unveraenderlich - ein Aufrufer kann
# dieses Modul-Konstante nicht versehentlich in-place mutieren und damit den Default fuer alle
# anderen `PiiScrubber`-Instanzen im selben Prozess verfaelschen. Bleibt in menschenlesbarer
# Schreibweise (mit Unterstrich) - die Trennzeichen-Normalisierung passiert erst beim
# tatsaechlichen Abgleich in `_build_denylist()`/`_is_denylisted()`.
DEFAULT_DENYLIST: frozenset[str] = frozenset(
    {"password", "token", "api_key", "secret", "authorization"}
)

# JSON-taugliche Blattwerte: haben per Definition keinen Feldnamen, gegen den sich die
# Denylist pruefen liesse, und werden daher immer unveraendert durchgereicht. `bool` ist in
# Python bereits eine `int`-Unterklasse (isinstance(True, int) is True) und stuende hier nur
# aus Gruenden der Lesbarkeit.
_JSON_SCALAR_TYPES = (str, int, float, bool, bytes, type(None))


def _normalize_field_name(name: object) -> str:
    """Bildet einen Feldnamen bzw. Denylist-Begriff auf eine trennzeichenfreie Vergleichsform ab.

    ``"api_key"``, ``"apiKey"`` und ``"x-api-key"`` werden alle zu ``"apikey"`` - siehe
    Moduldocstring fuer die Begruendung.
    """
    return str(name).strip().lower().replace("_", "").replace("-", "")


def _build_denylist(extra_denylist: Iterable[str]) -> frozenset[str]:
    normalized_extra: set[str] = set()
    for term in extra_denylist:
        if not isinstance(term, str):
            raise TypeError(
                "PiiScrubber: extra_denylist-Eintraege muessen str sein, erhalten "
                f"{term!r} ({type(term).__name__})."
            )
        normalized = _normalize_field_name(term)
        if not normalized:
            # Review-Fund (TF-851): ein leerer/nur aus Trennzeichen bestehender Begriff (z.B.
            # aus `extra_denylist=os.getenv("X", "").split(",")`) waere nach der Normalisierung
            # ein leerer String - und ein leerer String ist Teilstring JEDES Feldnamens. Ohne
            # diese Pruefung wuerde ein solcher Konfigurationsfehler den kompletten Payload
            # stillschweigend redigieren, statt sichtbar fehlzuschlagen.
            raise ValueError(
                "PiiScrubber: extra_denylist enthaelt einen leeren/nur aus Trennzeichen "
                f"bestehenden Begriff ({term!r}) - das wuerde jedes Feld redigieren."
            )
        normalized_extra.add(normalized)
    return frozenset({_normalize_field_name(term) for term in DEFAULT_DENYLIST} | normalized_extra)


def _is_denylisted(key: object, denylist: frozenset[str]) -> bool:
    normalized = _normalize_field_name(key)
    return any(term in normalized for term in denylist)


def _is_namedtuple(value: object) -> bool:
    # Namedtuples SIND `tuple`-Unterklassen (isinstance(value, tuple) ist True) - ohne diese
    # explizite Vorab-Erkennung wuerden sie in den generischen tuple-Zweig fallen, der rein
    # positional rekonstruiert (`tuple(...)`) und damit die Feldnamen verliert, gegen die sich
    # eigentlich die Denylist pruefen muesste (Review-Fund, TF-851: sah wie "unterstuetzt" aus,
    # redigierte aber nie etwas).
    return isinstance(value, tuple) and hasattr(value, "_asdict") and hasattr(value, "_fields")


def _scrub(value: object, denylist: frozenset[str], seen: frozenset[int]) -> object:
    if isinstance(value, _JSON_SCALAR_TYPES):
        return value

    # Zyklenerkennung ueber `id()` der bereits auf dem aktuellen Rekursionspfad befindlichen
    # Container (Review-Fund, TF-851): eine (versehentlich) selbstreferenzielle Struktur wuerde
    # sonst mit einem rohen, fuer den Aufrufer nichtssagenden `RecursionError` abbrechen statt
    # mit einer klaren, dem eigentlichen Problem zuordenbaren Fehlermeldung.
    obj_id = id(value)
    if obj_id in seen:
        raise ValueError(
            "PiiScrubber: zirkulaere Referenz erkannt - kann nicht rekursiv gescrubbt werden."
        )
    seen = seen | {obj_id}

    if isinstance(value, Mapping):
        return {
            key: REDACTED if _is_denylisted(key, denylist) else _scrub(val, denylist, seen)
            for key, val in value.items()
        }
    if _is_namedtuple(value):
        scrubbed_fields = {
            name: REDACTED if _is_denylisted(name, denylist) else _scrub(val, denylist, seen)
            for name, val in value._asdict().items()
        }
        return type(value)(**scrubbed_fields)
    if isinstance(value, list):
        return [_scrub(item, denylist, seen) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub(item, denylist, seen) for item in value)
    if isinstance(value, (set, frozenset)):
        return type(value)(_scrub(item, denylist, seen) for item in value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        updated_fields = {
            field.name: (
                REDACTED
                if _is_denylisted(field.name, denylist)
                else _scrub(getattr(value, field.name), denylist, seen)
            )
            for field in dataclasses.fields(value)
        }
        return dataclasses.replace(value, **updated_fields)

    # Fail closed statt fail open (Review-Fund, TF-851): ein nicht erkannter Objekttyp (z.B.
    # ein Pydantic-Modell oder eine beliebige eigene Klasse) koennte genauso sensible Felder
    # als Attribute tragen wie ein dict - ihn unveraendert durchzureichen waere ein stiller
    # PII-Leak, der von aussen nicht von "erfolgreich gescrubbt, keine sensiblen Felder
    # enthalten" zu unterscheiden waere.
    raise TypeError(
        f"PiiScrubber: Typ {type(value).__name__!r} kann nicht gescrubbt werden - unterstuetzt "
        "werden dict/list/tuple/set/frozenset/Dataclasses/Namedtuples sowie JSON-Skalare "
        "(str/int/float/bool/bytes/None). Vorher explizit in ein unterstuetztes Format "
        "konvertieren (z.B. .model_dump()/dataclasses.asdict())."
    )


class PiiScrubber:
    """Redigiert bekannte sensible Feldnamen in beliebig verschachtelten Strukturen.

    Eine Instanz buendelt eine (ggf. um ``extra_denylist`` erweiterte) Denylist fuer einen
    Aufrufer, der wiederholt gegen dieselbe Konfiguration scrubben will (z.B. `examcraft-
    private` mit produktspezifischen Zusatzfeldern) - die Denylist wird dabei einmalig beim
    Erzeugen normalisiert (und validiert, siehe ``_build_denylist()``) statt bei jedem
    ``scrub()``-Aufruf neu berechnet.
    """

    def __init__(self, *, extra_denylist: Iterable[str] = ()) -> None:
        self._denylist = _build_denylist(extra_denylist)

    @property
    def denylist(self) -> frozenset[str]:
        """Effektive, normalisierte (kleingeschrieben, ohne ``_``/``-``) Denylist dieser Instanz.

        Nur zur Introspektion (z.B. Tests/Debugging: "wird dieses Feld ueberhaupt redigiert?")
        - fuer die eigentliche Konfiguration ist der Konstruktor-Parameter ``extra_denylist``
        zustaendig.
        """
        return self._denylist

    def scrub(self, value: object) -> object:
        """Gibt eine neue, redigierte Kopie von ``value`` zurueck.

        ``value`` selbst (und alle enthaltenen dict/list/tuple/set/Dataclass/Namedtuple-
        Strukturen) bleiben unveraendert - Aufrufer koennen denselben Log-``extra``-Payload
        also gefahrlos sowohl lokal (unredigiert) als auch redigiert an einen geteilten
        Collector weiterreichen. Wirft ``TypeError`` fuer nicht unterstuetzte Objekttypen und
        ``ValueError`` bei einer zirkulaeren Referenz (siehe Moduldocstring).
        """
        return _scrub(value, self._denylist, frozenset())


def scrub_pii(value: object, *, extra_denylist: Iterable[str] = ()) -> object:
    """Komfortfunktion fuer einmaliges Scrubbing ohne eigene ``PiiScrubber``-Instanz.

    Aequivalent zu ``PiiScrubber(extra_denylist=extra_denylist).scrub(value)``. Fuer
    wiederholtes Scrubbing mit derselben (erweiterten) Denylist ist eine eigene
    ``PiiScrubber``-Instanz vorzuziehen, da diese Funktion die Denylist bei jedem Aufruf neu
    normalisiert und validiert.
    """
    return PiiScrubber(extra_denylist=extra_denylist).scrub(value)
