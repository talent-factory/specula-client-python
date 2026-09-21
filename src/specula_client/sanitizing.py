"""Sanitizing-Helfer fuer roh geloggten Freitext: URLs und Control-Chars.

Extrahiert (TF-892) aus `ratum`s `backend/app/routers/monitoring.py` (ADR-012) - dort bislang
punktuell fuer den unauthentifizierten Frontend-Fehler-Proxy-Endpoint gebaut, jetzt fuer einen
zweiten Konsumenten (`examcraft-private`, TF-864) hierher verschoben. Ergaenzt `scrubbing.py`
(TF-851): jenes Modul redigiert *strukturierte* dict/list-Payloads anhand bekannter Feldnamen,
dieses Modul saniert *freien Text* (URLs, Fehlermeldungen/Stacktraces), der keine benannten
Felder hat, gegen die sich eine Denylist pruefen liesse.

Beide Funktionen sind bewusst zustandslos (keine Konfiguration, kein `extra_denylist`-Aequivalent
wie bei `PiiScrubber`) - anders als die produktspezifisch erweiterbare Feldnamen-Denylist ist das
Muster (Query-String/Capability-Token in der URL, Steuerzeichen im Freitext) produktuebergreifend
identisch, siehe `ratum`s Originalimplementierung.
"""

from __future__ import annotations

import re

# Matcht Capability-Token direkt im URL-Pfad (nicht im Query-String) wie z.B. `/sign/:token` bei
# ratum - generisch genug gehalten, um `/sign/<beliebiges-token>` produktuebergreifend zu
# redigieren, ohne pro Produkt eine eigene Pfad-Konvention zu pflegen.
_SIGN_TOKEN_PATH = re.compile(r"/sign/[^/?#]+")

# Matcht ein optionales Schema gefolgt von `user[:pass]@` in der URL-Autoritaet (TF-895,
# z.B. "https://user:pass@host/..."). An den Stringanfang UND an Zeichen vor dem ersten "/"
# gebunden (`[^/@]*`), damit niemals ein unbeteiligtes "@" weiter hinten im Pfad getroffen
# wird (z.B. "https://example.com/path@2x.png" muss unveraendert bleiben - dort steht "/"
# vor dem "@", also bricht das Matching vorher ab statt ueber die Pfadgrenze zu "springen").
_USERINFO = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)?[^/@]*@")

# Alle ASCII-Steuerzeichen ausser Tab (\x09) - Tab ist in einer Log-Zeile harmlos, \r/\n und die
# uebrigen C0-Controls koennen dagegen zeilenorientierte Log-Formatter (Fly-/Konsolen-Logs) fuer
# Log-Injection missbrauchen (siehe ratum-Originaldocstring).
_CONTROL_CHARS = re.compile(r"[\r\n\x00-\x08\x0b\x0c\x0e-\x1f]")


def sanitize_url(url: str) -> str:
    """Redigiert eine roh vom Client gelieferte URL vor dem Loggen.

    Strippt Query-String und Fragment pauschal (koennen Capability-Token tragen, z.B.
    `/verify-email?token=...`/`/reset-password?token=...`), entfernt Userinfo-Credentials aus
    der URL-Autoritaet (`https://user:pass@host/...`, TF-895) und redigiert zusaetzlich
    `/sign/:token`-artige Pfad-Segmente, bei denen das Token selbst Teil des Pfads statt des
    Query-Strings ist. Gibt bei einem bereits sauberen `url`-Wert die (unveraenderte) URL ohne
    Query-String/Fragment zurueck.
    """
    without_query = url.split("?", 1)[0].split("#", 1)[0]
    without_userinfo = _USERINFO.sub(r"\1", without_query)
    return _SIGN_TOKEN_PATH.sub("/sign/<redacted>", without_userinfo)


def strip_control_chars(text: str) -> str:
    """Ersetzt Newlines/Steuerzeichen in `text` durch ein Leerzeichen (Log-Injection-Schutz).

    Ohne diese Sanitisierung koennte ein Aufrufer eines unauthentifizierten Endpoints (der
    typische Konsument dieser Funktion) ueber Newlines in einem Freitextfeld beliebige,
    gefaelschte zusaetzliche Log-Zeilen in zeilenorientierte Formatter einschleusen - z.B. eine
    fabrizierte `ERROR app.security: ...`-Zeile, die Forensik/Incident-Response untergraebt. Der
    Inhalt selbst bleibt (best-effort) sichtbar, landet aber garantiert als Teil der urspruenglich
    einen Log-Zeile, nie als eigenstaendige neue Zeile.
    """
    return _CONTROL_CHARS.sub(" ", text)
