"""Apps that ``neolith run`` and ``neolith migrate`` host in the tests of
``tests/test_run.py``, each in a process of its own.

``desk`` keeps notes in its own database and calls its own contract from its
views; ``failing`` has a migration that raises; ``needs`` declares a battery
with tables, which a host started from the command line refuses.
"""
