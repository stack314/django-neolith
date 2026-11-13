"""Admin code in a project module, in no installed app."""

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse

from tests import request_site as site
from tests.request_admin.shop.models import Crate, Item


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    pass


def project_quote(
    modeladmin: admin.ModelAdmin,  # type: ignore[type-arg]
    request: HttpRequest,
    queryset: QuerySet[Crate],
) -> HttpResponse:
    """A project function given to the admin as an option."""
    return HttpResponse(site.ask())


admin.site.register(Crate, actions=(project_quote,))
