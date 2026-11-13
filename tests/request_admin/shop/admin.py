from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse

from tests import request_site as site
from tests.request_admin.shop.models import Basket, Order, Shelf


def quote(
    modeladmin: admin.ModelAdmin,  # type: ignore[type-arg]
    request: HttpRequest,
    queryset: QuerySet[Order],
) -> HttpResponse:
    """An admin action that calls a contract and answers with what came of it."""
    return HttpResponse(site.ask())


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    actions = (quote,)


admin.site.register(Basket)
# Options make Django build a class of its own, in django.contrib.admin.sites.
admin.site.register(Shelf, list_display=("name",))
admin.site.add_action(quote, "quote_everywhere")

# A project-level admin module, loaded while the apps load, as one imported
# from an app's admin module is.
from tests.request_admin import project_admin  # noqa: E402, F401
