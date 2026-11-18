from django.db import models


class Note(models.Model):
    text = models.CharField(max_length=40)

    def __str__(self) -> str:
        return str(self.text)
