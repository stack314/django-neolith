"""An admin class in a project module, in no installed app."""

from django.contrib import admin

from tests.request_admin.shop.models import Item


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    pass
