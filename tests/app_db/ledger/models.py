from django.db import models


class Tag(models.Model):
    name = models.CharField(max_length=20)


class Account(models.Model):
    name = models.CharField(max_length=20)
    tags = models.ManyToManyField(Tag, blank=True)


class Entry(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE)
    amount = models.IntegerField()
