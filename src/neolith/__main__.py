"""``python -m neolith``: the ``neolith`` command line (ADR 0022 Decision 12)."""

import sys

from neolith._cli import main

if __name__ == "__main__":
    sys.exit(main())
