from django.contrib.auth.models import Permission
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


class Shelf(models.Model):
    """Served by an admin class of the depot app."""

    name = models.CharField(max_length=20)

    def __str__(self) -> str:
        return self.name


class Crate(models.Model):
    """Served by the class Django builds for register(Crate, **options)."""

    name = models.CharField(max_length=20)

    def __str__(self) -> str:
        return self.name


class Team(models.Model):
    """Served by ``django.contrib.auth``'s GroupAdmin."""

    name = models.CharField(max_length=20)
    permissions = models.ManyToManyField(Permission, blank=True)

    def __str__(self) -> str:
        return self.name
