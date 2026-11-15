"""A URLconf in no installed app that registers an admin class of its own,
which only Boot's second run of ``neolith.W001`` sees (spec 05, APP-26)."""

from django.contrib import admin
from django.contrib.contenttypes.models import ContentType

site = admin.AdminSite(name="boot")


class ProjectAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """An admin class in a project module, so in no installed app."""


site.register(ContentType, ProjectAdmin)

urlpatterns: list[object] = []
