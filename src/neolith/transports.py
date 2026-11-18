"""The transports a remote provider's entry names (ADR 0014 Decision 4).

A remote provider's entry in ``NEOLITH["REMOTE_PROVIDERS"]`` names its
transport under ``BACKEND``, and in 0.1 the one value admitted is
``"neolith.transports.HTTPTransport"``, compared as written; the key joins
the host's settings with remote providers, later in roadmap step 5. The
name is for that setting: the class's constructor and methods are private
in 0.1, so an adopter never builds or subclasses it (ADR 0014 Decision 7).
"""

from neolith._http_transport import HTTPTransport

__all__ = ["HTTPTransport"]
