from django.db import models


class Note(models.Model):
    """A model in an app that declares no database resource."""

    text = models.CharField(max_length=20)

    def __str__(self) -> str:
        return str(self.text)
