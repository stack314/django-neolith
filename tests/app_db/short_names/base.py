from django.db.backends.sqlite3 import base, operations

LIMIT = 10


class DatabaseOperations(operations.DatabaseOperations):
    def max_name_length(self) -> int:
        return LIMIT


class DatabaseWrapper(base.DatabaseWrapper):
    ops_class = DatabaseOperations
