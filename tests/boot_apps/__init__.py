"""Hosted apps with a database whose migrations Boot reads (ADR 0022 Decision 7).

- ``fresh``: two migrations, the second depending on the first;
- ``bare``: a database and no migrations;
- ``forked``: two leaf migrations, which conflict;
- ``leaning``: a migration that depends on one of ``fresh``;
- ``empty``: a migrations package with no migration in it, as ``startapp``
  leaves it;
- ``dangling``: a migration that depends on one that does not exist;
- ``squashed``: two migrations, a squash that replaces them, and a third.

None has models: Boot reads migration records, not tables.
"""
