from django.db import models


class Order(models.Model):
    """Served by an admin class of this app."""

    name = models.CharField(max_length=20)

    def __str__(self) -> str:
        return self.name


class Basket(models.Model):
    """Served by the stock ModelAdmin, which runs no app's code."""

    name = models.CharField(max_length=20)

    def __str__(self) -> str:
        return self.name


class Item(models.Model):
    """Served by an admin class in a project module."""

    name = models.CharField(max_length=20)

    def __str__(self) -> str:
        return self.name
