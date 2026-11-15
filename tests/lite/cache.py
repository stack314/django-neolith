from django.core.cache.backends.db import DatabaseCache


class ProjectCache(DatabaseCache):
    """A project's subclass of Django's database cache."""
