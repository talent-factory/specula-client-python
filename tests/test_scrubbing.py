"""Tests fuer specula_client.scrubbing (TF-851)."""

import dataclasses
from collections import namedtuple
from types import MappingProxyType

import pytest

from specula_client.scrubbing import (
    DEFAULT_DENYLIST,
    REDACTED,
    SPECULA_EXTRA_PREFIX,
    PiiScrubber,
    scrub_pii,
)


@pytest.mark.parametrize("field", ["password", "token", "api_key", "secret", "authorization"])
def test_default_denylist_covers_minimum_required_fields(field):
    # AC1: die in der Linear-Beschreibung geforderte Mindest-Denylist muss Teil des Defaults
    # sein, nicht nur optional per extra_denylist nachruestbar.
    assert field in DEFAULT_DENYLIST


def test_specula_extra_prefix_matches_convention_used_by_log_extras():
    # TF-937: einzige Quelle der Wahrheit fuer die "specula_"-Extra-Konvention, die sowohl
    # `SpeculaLogHandler.emit()` (specula_client.logging) als auch Konsumenten (z.B. ratums
    # `PiiScrubbingLogFilter`) auswerten - ein kuenftiger Library-Bump mit geaenderter
    # Konvention aendert damit beide Stellen gemeinsam statt eine davon stillschweigend
    # zurueckzulassen.
    assert SPECULA_EXTRA_PREFIX == "specula_"


def test_scrub_pii_redacts_top_level_denylisted_field():
    result = scrub_pii({"password": "hunter2", "username": "daniel"})

    assert result == {"password": REDACTED, "username": "daniel"}


def test_scrub_pii_redacts_arbitrarily_nested_denylisted_fields():
    # AC1: "in beliebiger Verschachtelungstiefe" - hier ueber vier Ebenen dict-in-dict.
    payload = {
        "user": {
            "profile": {
                "credentials": {"password": "hunter2", "api_key": "sk-live-abc"},
                "email": "daniel@example.com",
            }
        }
    }

    result = scrub_pii(payload)

    assert result["user"]["profile"]["credentials"] == {
        "password": REDACTED,
        "api_key": REDACTED,
    }
    assert result["user"]["profile"]["email"] == "daniel@example.com"


def test_scrub_pii_redacts_denylisted_fields_inside_list_of_dicts():
    # AC3-Edge-Case: Listen von Dicts, z.B. mehrere Log-Eintraege oder Request-Payloads.
    payload = [
        {"token": "abc123", "action": "login"},
        {"token": "def456", "action": "logout"},
    ]

    result = scrub_pii(payload)

    assert result == [
        {"token": REDACTED, "action": "login"},
        {"token": REDACTED, "action": "logout"},
    ]


def test_scrub_pii_scrubs_dicts_nested_inside_lists_nested_inside_dicts():
    payload = {"events": [{"headers": {"Authorization": "Bearer abc"}}]}

    result = scrub_pii(payload)

    assert result["events"][0]["headers"] == {"Authorization": REDACTED}


def test_scrub_pii_matches_denylist_terms_case_insensitively():
    result = scrub_pii({"Password": "hunter2", "API_KEY": "sk-live-abc", "Secret": "s"})

    assert result == {"Password": REDACTED, "API_KEY": REDACTED, "Secret": REDACTED}


def test_scrub_pii_matches_denylist_terms_as_substring_of_field_name():
    # Bewusstes Design (siehe Moduldocstring): Feldnamen wie "auth_token"/"userPassword" sind
    # in der Praxis haeufiger als exakte Denylist-Treffer - ein False Negative waere hier das
    # groessere Risiko als ein False Positive.
    result = scrub_pii({"auth_token": "abc", "userPassword": "hunter2", "x-api-key": "sk-live"})

    assert result == {"auth_token": REDACTED, "userPassword": REDACTED, "x-api-key": REDACTED}


def test_scrub_pii_leaves_non_denylisted_fields_unchanged():
    result = scrub_pii({"username": "daniel", "count": 3, "active": True})

    assert result == {"username": "daniel", "count": 3, "active": True}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, {}),
        ([], []),
        ({"password": ""}, {"password": REDACTED}),
        ({"password": None}, {"password": REDACTED}),
        ("just a string", "just a string"),
        (None, None),
        (0, 0),
    ],
)
def test_scrub_pii_handles_empty_and_scalar_edge_cases(payload, expected):
    # AC3-Edge-Case "leere Werte": ein denylisteter Key mit leerem/None-Wert muss weiterhin
    # redigiert werden - sonst liesse sich aus "Feld vorhanden aber leer" vs. "Feld redigiert"
    # indirekt ablesen, ob je ein Wert gesetzt war.
    assert scrub_pii(payload) == expected


def test_scrub_pii_preserves_control_characters_in_non_denylisted_values():
    # AC3-Edge-Case "Control-Chars": Werte mit Steuerzeichen (z.B. aus rohen Request-Bodies)
    # duerfen den rekursiven Scrub weder zum Absturz bringen noch veraendert werden - Scrubbing
    # entscheidet ausschliesslich anhand des Feldnamens, nicht des Wertinhalts.
    payload = {"raw_body": "line1\r\nline2\x00\x1b[31mred\x1b[0m", "password": "hunter2"}

    result = scrub_pii(payload)

    assert result["raw_body"] == "line1\r\nline2\x00\x1b[31mred\x1b[0m"
    assert result["password"] == REDACTED


def test_scrub_pii_redacts_denylisted_field_whose_value_itself_contains_control_characters():
    result = scrub_pii({"password": "hunter2\x00\r\n"})

    assert result == {"password": REDACTED}


def test_scrub_pii_does_not_mutate_the_input():
    payload = {"password": "hunter2", "nested": {"token": "abc"}}
    original = {"password": "hunter2", "nested": {"token": "abc"}}

    scrub_pii(payload)

    assert payload == original


def test_scrub_pii_accepts_extra_denylist_without_mutating_default_denylist():
    # AC2: die Denylist ist pro Aufrufer erweiterbar (z.B. examcraft-private-spezifische
    # Zusatzfelder), ohne den globalen Default fuer andere Aufrufer im selben Prozess zu
    # veraendern.
    result = scrub_pii({"ssn": "123-45-6789", "password": "hunter2"}, extra_denylist=["ssn"])

    assert result == {"ssn": REDACTED, "password": REDACTED}
    assert "ssn" not in DEFAULT_DENYLIST


def test_pii_scrubber_reuses_configured_extra_denylist_across_calls():
    scrubber = PiiScrubber(extra_denylist=["employee_id"])

    first = scrubber.scrub({"employee_id": "42", "name": "Daniel"})
    second = scrubber.scrub({"employee_id": "43", "password": "hunter2"})

    assert first == {"employee_id": REDACTED, "name": "Daniel"}
    assert second == {"employee_id": REDACTED, "password": REDACTED}


def test_pii_scrubber_without_extra_denylist_behaves_like_default():
    assert PiiScrubber().scrub({"password": "hunter2"}) == {"password": REDACTED}


def test_scrub_pii_redacts_denylisted_fields_with_non_string_keys():
    # dict-Keys sind in JSON immer Strings, aber Python-dicts (z.B. aus internem Code vor der
    # JSON-Serialisierung) koennen beliebige hashbare Keys haben - str(key) darf hier nicht
    # crashen.
    result = scrub_pii({42: "some value", "password": "hunter2"})

    assert result == {42: "some value", "password": REDACTED}


def test_scrub_pii_preserves_tuples_as_tuples():
    result = scrub_pii(({"password": "hunter2"}, {"username": "daniel"}))

    assert result == ({"password": REDACTED}, {"username": "daniel"})


def test_scrub_pii_matches_denylist_terms_across_lists_of_lists():
    result = scrub_pii([[{"token": "abc"}], [{"name": "daniel"}]])

    assert result == [[{"token": REDACTED}], [{"name": "daniel"}]]


@pytest.mark.parametrize("value", ["hunter2", 1234, 12.5, True])
def test_scrub_pii_redacts_denylisted_field_regardless_of_value_type(value):
    assert scrub_pii({"password": value}) == {"password": REDACTED}


def test_scrub_pii_matches_denylist_terms_in_unicode_field_names():
    result = scrub_pii({"Paßwort_token": "geheim", "Straße": "Hauptstrasse 1"})

    assert result == {"Paßwort_token": REDACTED, "Straße": "Hauptstrasse 1"}


class TestSeparatorNormalization:
    """Regressionstests fuer den Review-Fund: Vergleich muss `_`/`-` auf beiden Seiten
    (Feldname UND Denylist-Begriff) entfernen, sonst matchen camelCase-Feldnamen und per
    `extra_denylist` uebergebene Bindestrich-Begriffe nie."""

    def test_camel_case_field_name_matches_snake_case_default_term(self):
        # Vorher ein False Negative: "apiKey".lower() == "apikey" enthielt "api_key" nicht.
        result = scrub_pii({"apiKey": "sk-live-abc", "username": "daniel"})

        assert result == {"apiKey": REDACTED, "username": "daniel"}

    def test_hyphenated_extra_denylist_term_matches_hyphenated_field_name(self):
        # Vorher ein False Negative: nur der Feldname wurde "-" -> "_" normalisiert, der
        # extra_denylist-Begriff selbst nicht - "session-id" traf daher nie auf "session-id".
        result = scrub_pii({"session-id": "abc123"}, extra_denylist=["session-id"])

        assert result == {"session-id": REDACTED}

    def test_extra_denylist_term_matches_field_name_case_insensitively(self):
        result = scrub_pii({"SSN": "123-45-6789"}, extra_denylist=["Ssn"])

        assert result == {"SSN": REDACTED}


class TestExtraDenylistValidation:
    """Review-Fund: ein leerer/nur aus Trennzeichen bestehender oder nicht-string
    extra_denylist-Eintrag darf nicht stillschweigend zu 'alles wird redigiert' oder einem
    kryptischen AttributeError fuehren, sondern muss beim Erzeugen fail-fast sein."""

    @pytest.mark.parametrize("blank_term", ["", "  ", "_", "-", "_-_"])
    def test_blank_extra_denylist_term_raises_value_error(self, blank_term):
        with pytest.raises(ValueError, match="leeren"):
            PiiScrubber(extra_denylist=[blank_term])

    def test_blank_extra_denylist_term_via_scrub_pii_raises_before_any_scrubbing(self):
        with pytest.raises(ValueError, match="leeren"):
            scrub_pii({"password": "hunter2"}, extra_denylist=[""])

    def test_non_string_extra_denylist_entry_raises_type_error(self):
        with pytest.raises(TypeError, match="str"):
            PiiScrubber(extra_denylist=[None])


def test_scrub_pii_redacts_field_names_that_are_substrings_of_denylist_terms_intentional_tradeoff():
    # Bewusster, im Moduldocstring dokumentierter Trade-off: die Substring-Matching-Strategie
    # redigiert auch harmlose Felder wie "total_tokens" (LLM-Token-Zaehler) mit, weil sie
    # "token" enthalten. Dieser Test haelt das Verhalten als beabsichtigt fest, statt es als
    # ueberraschenden Bugfund erneut zu melden.
    result = scrub_pii({"total_tokens": 42, "prompt_tokens": 10})

    assert result == {"total_tokens": REDACTED, "prompt_tokens": REDACTED}


def test_pii_scrubber_denylist_property_exposes_normalized_effective_denylist():
    scrubber = PiiScrubber(extra_denylist=["employee-id"])

    assert "employeeid" in scrubber.denylist
    assert "password" in scrubber.denylist


def test_two_pii_scrubber_instances_do_not_leak_extra_denylist_between_each_other():
    with_extra = PiiScrubber(extra_denylist=["employee_id"])
    without_extra = PiiScrubber()

    assert with_extra.scrub({"employee_id": "42"}) == {"employee_id": REDACTED}
    assert without_extra.scrub({"employee_id": "42"}) == {"employee_id": "42"}


def test_scrub_pii_redacts_denylisted_key_in_a_mapping_proxy_type():
    # AC1: die Implementierung prueft bewusst `isinstance(value, Mapping)` (nicht `dict`), um
    # beliebige Mapping-Typen zu unterstuetzen - dieser Test exerziert diese Abstraktion statt
    # nur `dict`-Literale.
    payload = MappingProxyType({"password": "hunter2", "username": "daniel"})

    result = scrub_pii(payload)

    assert result == {"password": REDACTED, "username": "daniel"}
    assert isinstance(result, dict)


class TestDataclassSupport:
    @dataclasses.dataclass
    class _Credentials:
        password: str
        username: str

    @dataclasses.dataclass
    class _Nested:
        credentials: "TestDataclassSupport._Credentials"
        label: str

    def test_scrub_pii_redacts_denylisted_dataclass_field(self):
        payload = self._Credentials(password="hunter2", username="daniel")

        result = scrub_pii(payload)

        assert result == self._Credentials(password=REDACTED, username="daniel")

    def test_scrub_pii_recurses_into_nested_dataclass_fields(self):
        payload = self._Nested(
            credentials=self._Credentials(password="hunter2", username="daniel"),
            label="login-attempt",
        )

        result = scrub_pii(payload)

        assert result.credentials == self._Credentials(password=REDACTED, username="daniel")
        assert result.label == "login-attempt"


class TestNamedtupleSupport:
    _Credentials = namedtuple("_Credentials", ["password", "username"])

    def test_scrub_pii_redacts_denylisted_namedtuple_field_and_preserves_type(self):
        payload = self._Credentials(password="hunter2", username="daniel")

        result = scrub_pii(payload)

        assert result == self._Credentials(password=REDACTED, username="daniel")
        assert isinstance(result, self._Credentials)

    def test_scrub_pii_does_not_silently_lose_namedtuple_field_names(self):
        # Regressionstest fuer den Review-Fund: vor dem Fix wurde ein Namedtuple ueber den
        # generischen tuple-Zweig rein positional rekonstruiert und verlor damit die
        # Feldnamen, gegen die sich die Denylist eigentlich haette pruefen muessen - der Wert
        # blieb unredigiert, sah aber "verarbeitet" aus.
        payload = self._Credentials(password="hunter2", username="daniel")

        result = scrub_pii(payload)

        assert result.password == REDACTED


def test_scrub_pii_scrubs_elements_inside_a_set_and_preserves_the_set_type():
    result = scrub_pii({"tags", "labels", "notes"})

    assert result == {"tags", "labels", "notes"}
    assert isinstance(result, set)


def test_scrub_pii_scrubs_elements_inside_a_frozenset_and_preserves_the_frozenset_type():
    result = scrub_pii(frozenset({"a", "b"}))

    assert result == frozenset({"a", "b"})
    assert isinstance(result, frozenset)


class TestUnsupportedTypesFailClosed:
    """Review-Fund (Kernstueck des Security-Fixes): ein nicht erkannter Objekttyp (z.B. ein
    Pydantic-Modell) darf niemals still unredigiert durchgereicht werden - das waere ein
    stiller PII-Leak. `scrub()`/`scrub_pii()` muessen stattdessen laut fehlschlagen."""

    class _ArbitraryObjectWithAPasswordAttribute:
        def __init__(self):
            self.password = "hunter2"

    def test_scrub_pii_raises_type_error_for_an_unrecognized_object_type(self):
        with pytest.raises(TypeError, match="_ArbitraryObjectWithAPasswordAttribute"):
            scrub_pii(self._ArbitraryObjectWithAPasswordAttribute())

    def test_scrub_pii_raises_type_error_for_an_unrecognized_type_nested_in_a_dict(self):
        # Der gefaehrlichste Fall: das unerkannte Objekt steckt tief in einer sonst
        # unterstuetzten Struktur - darf trotzdem nicht durchrutschen.
        payload = {"user": {"request": self._ArbitraryObjectWithAPasswordAttribute()}}

        with pytest.raises(TypeError):
            scrub_pii(payload)


def test_scrub_pii_raises_value_error_for_a_circular_reference():
    payload = {"name": "daniel"}
    payload["self"] = payload

    with pytest.raises(ValueError, match="zirkulaere"):
        scrub_pii(payload)
