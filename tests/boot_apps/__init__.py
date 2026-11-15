"""Hosted apps with a database whose migrations Boot reads (ADR 0022 Decision 7).

- ``fresh``: two migrations, the second depending on the first;
- ``bare``: a database and no migrations;
- ``forked``: two leaf migrations, which conflict;
- ``leaning``: a migration that depends on one of ``fresh``.

None has models: Boot reads migration records, not tables.
"""
