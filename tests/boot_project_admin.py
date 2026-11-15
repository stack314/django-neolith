"""A project module, in no installed app, that registers an admin class
while apps load."""

from django.contrib import admin
from django.contrib.contenttypes.models import ContentType

site = admin.AdminSite(name="loading")


class LoadingAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """An admin class in a project module, registered at set-up."""


site.register(ContentType, LoadingAdmin)
