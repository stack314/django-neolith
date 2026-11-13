from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse

from tests import request_site as site
from tests.request_admin import project_admin
from tests.request_admin.depot.models import Pallet
from tests.request_admin.shop.models import Basket, Order, Team


def quote(
    modeladmin: admin.ModelAdmin,  # type: ignore[type-arg]
    request: HttpRequest,
    queryset: QuerySet[Order],
) -> HttpResponse:
    """An admin action that calls a contract and answers with what came of it."""
    return HttpResponse(site.ask())


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    # A project function among its actions: the class is the shop app's.
    actions = (quote, project_admin.project_quote)


@admin.register(Pallet)
class PalletAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """The shop app's admin class for a model of the depot app."""


admin.site.register(Basket)
# An admin class of an app with no declaration, from Django itself.
admin.site.register(Team, GroupAdmin)  # type: ignore[misc]
admin.site.add_action(quote, "quote_everywhere")
