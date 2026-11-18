"""The user model of ``tests.principal_apps.textkey``."""

from django.contrib.auth.models import AbstractBaseUser
from django.db import models


class User(AbstractBaseUser):
    username = models.CharField(max_length=40, primary_key=True)

    USERNAME_FIELD = "username"
