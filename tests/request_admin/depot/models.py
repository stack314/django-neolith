from django.db import models


class Pallet(models.Model):
    """Served by an admin class of the shop app."""

    name = models.CharField(max_length=20)

    def __str__(self) -> str:
        return self.name


class Bin(models.Model):
    """Served by the stock ModelAdmin."""

    name = models.CharField(max_length=20)

    def __str__(self) -> str:
        return self.name
