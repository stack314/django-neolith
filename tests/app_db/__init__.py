"""Apps for the app database tests (spec 05, An app's database).

- ``ledger`` and ``billing``: hosted apps with a database, models and
  migrations;
- ``notes``: a hosted app with no database and no models;
- ``archive``: a hosted app with a database and no models module, for
  models a test defines in an isolated registry;
- ``legacy``: an app with no declaration, with models, which lives on a
  real ``default``;
- ``stray``: a hosted app with models and no database, which the host
  refuses;
- ``short_names``: a SQLite backend whose names are cut at 10 characters.
"""
