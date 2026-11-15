from django.db.backends.sqlite3 import base


class DatabaseWrapper(base.DatabaseWrapper):
    """Django's SQLite connection, as a project subclasses it."""
