from django.contrib import admin

from tests.request_admin.depot.models import Bin
from tests.request_admin.shop.models import Shelf

admin.site.register(Bin)


@admin.register(Shelf)
class ShelfAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """The depot app's admin class for a model of the shop app."""
