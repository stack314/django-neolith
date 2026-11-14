from django.conf import settings
from django.db import models


class Memo(models.Model):
    text = models.CharField(max_length=20)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
