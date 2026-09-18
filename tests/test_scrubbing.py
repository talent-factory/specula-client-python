"""Tests fuer specula_client.scrubbing (TF-851)."""

import pytest

from specula_client.scrubbing import DEFAULT_DENYLIST, REDACTED, PiiScrubber, scrub_pii


@pytest.mark.parametrize("field", ["password", "token", "api_key", "secret", "authorization"])
def test_default_denylist_covers_minimum_required_fields(field):
    # AC1: die in der Linear-Beschreibung geforderte Mindest-Denylist muss Teil des Defaults
    # sein, nicht nur optional per extra_denylist nachruestbar.
    assert field in DEFAULT_DENYLIST


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
