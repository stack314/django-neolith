from django.db import models


class Tag(models.Model):
    name = models.CharField(max_length=20)

    def __str__(self) -> str:
        return str(self.name)


class Account(models.Model):
    name = models.CharField(max_length=20)
    tags = models.ManyToManyField(Tag, blank=True)

    def __str__(self) -> str:
        return str(self.name)


class Entry(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE)
    amount = models.IntegerField()

    def __str__(self) -> str:
        return str(self.amount)
