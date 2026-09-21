"""Tests fuer specula_client.sanitizing (TF-892, TF-895)."""

from specula_client.sanitizing import sanitize_url, strip_control_chars


def test_sanitize_url_strips_query_string():
    # Query-Strings koennen Capability-Token tragen (z.B. /reset-password?token=...).
    result = sanitize_url("https://example.com/reset-password?token=super-secret")

    assert result == "https://example.com/reset-password"
    assert "super-secret" not in result


def test_sanitize_url_strips_fragment():
    result = sanitize_url("https://example.com/dashboard#section=billing")

    assert result == "https://example.com/dashboard"


def test_sanitize_url_redacts_sign_token_path():
    # /sign/:token traegt die Signatur-Capability direkt im Pfad, nicht im Query-String.
    result = sanitize_url("https://example.com/sign/abc123def")

    assert result == "https://example.com/sign/<redacted>"
    assert "abc123def" not in result


def test_sanitize_url_leaves_clean_url_unchanged():
    result = sanitize_url("https://example.com/dashboard")

    assert result == "https://example.com/dashboard"


def test_sanitize_url_strips_userinfo_credentials():
    # TF-895: user[:pass]@ in der URL-Autoritaet ist ein Log-Leak-Vektor, den Query-String-
    # und /sign/:token-Redacting allein nicht abdecken.
    result = sanitize_url("https://user:secret@example.com/path")

    assert result == "https://example.com/path"
    assert "secret" not in result


def test_sanitize_url_strips_userinfo_without_password():
    result = sanitize_url("https://user@example.com/path")

    assert result == "https://example.com/path"


def test_sanitize_url_strips_userinfo_without_scheme():
    result = sanitize_url("user:secret@example.com/path")

    assert result == "example.com/path"
    assert "secret" not in result


def test_sanitize_url_does_not_treat_an_at_sign_in_the_path_as_userinfo():
    # Ein "@" nach dem ersten "/" gehoert zum Pfad, nicht zur Autoritaet - z.B. eine
    # Bild-Datei mit "@2x" im Namen darf nicht faelschlich als Userinfo-Praefix gelesen werden.
    result = sanitize_url("https://example.com/path@2x.png")

    assert result == "https://example.com/path@2x.png"


def test_sanitize_url_combines_userinfo_and_sign_token_redacting():
    result = sanitize_url("https://user:secret@example.com/sign/abc123def")

    assert result == "https://example.com/sign/<redacted>"
    assert "secret" not in result
    assert "abc123def" not in result


def test_strip_control_chars_neutralizes_newlines():
    # Ohne Sanitisierung koennte ein Aufrufer eine gefaelschte zusaetzliche Log-Zeile
    # einschleusen (Log-Injection).
    result = strip_control_chars("boom\nERROR app.security: fake admin login from 1.2.3.4")

    assert "\n" not in result
    assert not any(line.strip().startswith("ERROR app.security") for line in result.splitlines())
    # Inhalt bleibt (best-effort) sichtbar, nur nicht mehr als eigenstaendige Zeile.
    assert "fake admin login" in result


def test_strip_control_chars_neutralizes_carriage_return():
    result = strip_control_chars("at foo\r\nat bar")

    assert "\r" not in result
    assert "\n" not in result


def test_strip_control_chars_leaves_plain_text_unchanged():
    result = strip_control_chars("TypeError: x is undefined")

    assert result == "TypeError: x is undefined"


def test_strip_control_chars_keeps_tab():
    # Tab ist in einer Log-Zeile harmlos, kein Injection-Vektor.
    result = strip_control_chars("a\tb")

    assert result == "a\tb"
