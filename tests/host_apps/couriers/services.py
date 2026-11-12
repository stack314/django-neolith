"""The couriers app's implementation: it quotes the length of the SKU."""

from neolith import CallContext, Principal
from tests.declaration_contracts import Level, Sku

# Every instance built, and each call's caller with the instance that served
# it, so a test can tell which set-up built what and which app called.
BUILT: list["CourierService"] = []
CALLS: list[tuple[str | None, "CourierService"]] = []
# Each call's caller and the principal it carried.
SEEN: list[tuple[str | None, Principal | None]] = []
# Each call's whole context.
CONTEXTS: list[CallContext] = []


class CourierService:
    def __init__(self) -> None:
        BUILT.append(self)

    def quote(self, request: Sku, ctx: CallContext) -> Level:
        CALLS.append((ctx.caller, self))
        SEEN.append((ctx.caller, ctx.principal))
        CONTEXTS.append(ctx)
        return Level(on_hand=len(request.sku))
