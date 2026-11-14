from django.db import models


class Invoice(models.Model):
    number = models.CharField(max_length=20)

    def __str__(self) -> str:
        return str(self.number)
