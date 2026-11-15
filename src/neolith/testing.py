"""Helpers for a test suite of a neolith host (ADR 0022 Decision 7)."""

from neolith import _boot

__all__ = ["boot"]


def boot() -> None:
    """Boot the host for a test session, without reading any schema.

    Django's test runner builds each test database itself, so the schema read
    of Boot would look at databases the run never created. Call this once, in
    a session fixture, before the first handler is built: it checks
    ``ROOT_URLCONF`` as Boot does and marks the host booted, so every handler
    built afterwards skips the schema read, including the checks that refuse
    an in-memory database. It is the one way to turn that read off, and is
    meant for tests only.

    Raises:
        UsageError: If the host is not set up, or Boot has already run in
            this process.
        ImproperlyConfigured: If ``ROOT_URLCONF`` is not set or does not
            import.
    """
    _boot.boot_for_tests()
