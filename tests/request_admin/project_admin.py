"""Admin code in a project module, in no installed app. The shop app's admin
module imports it, so it is loaded while the apps load.
"""

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse

from tests import request_site as site
from tests.request_admin.shop.models import Crate, Item


def project_quote(
    modeladmin: admin.ModelAdmin,  # type: ignore[type-arg]
    request: HttpRequest,
    queryset: QuerySet[Crate],
) -> HttpResponse:
    """A project function used as an admin action."""
    return HttpResponse(site.ask())


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    pass


# Options are configuration of the class Django builds: the model's app.
admin.site.register(Crate, actions=(project_quote,))

# A second admin site, with a project admin class of its own.
other_site = admin.AdminSite(name="other")


class OtherItemAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    pass


other_site.register(Item, OtherItemAdmin)
