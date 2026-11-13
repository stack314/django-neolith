"""A feed of the shop app: a framework view class subclassed by app code."""

from django.contrib.syndication.views import Feed


class OrderFeed(Feed):  # type: ignore[type-arg]
    title = "orders"
    link = "/orders/"

    def items(self) -> list[object]:
        return []
