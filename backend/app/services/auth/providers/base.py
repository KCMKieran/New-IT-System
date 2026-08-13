"""The one type every identity provider produces (auth design P3, §3).

``cf_access.py`` (route A, shelved) and ``email_otp.py`` (P6) converge on this
same shape, which is why ``auth_service.login()`` needs no knowledge of how the
identity was established.
"""

from __future__ import annotations

from dataclasses import dataclass


class ProviderError(Exception):
    """The identity could not be established.

    ``code`` is a short stable token (``no_email_claim``, ``state_expired``, …)
    that is safe to put in a redirect query string and to grep for in
    auth_events; ``str(exc)`` carries the operator-facing detail and must NOT be
    shown to the browser.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AuthenticatedIdentity:
    """A person, as proven by some provider. Not yet a session."""

    email: str
    display_name: str | None
    source: str
