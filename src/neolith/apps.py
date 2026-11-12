"""neolith's own Django app, which sets the host up (ADR 0022 Decision 5).

Install it as ``"neolith"``, after every app it hosts.
"""

from neolith._host import HostConfig

__all__ = ["HostConfig"]
