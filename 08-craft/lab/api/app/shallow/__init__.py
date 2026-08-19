"""Topic 1: the four-layer shape, built to be realistically shallow.

WHAT THIS DEMONSTRATES: `router -> service -> repository -> dao`, the default in
Python service codebases, with each layer declaring its OWN data-transfer type.
That last part is what makes the change amplification real rather than
theatrical: a one-field requirement touches a type per layer before it touches
any logic. A four-layer app that passes one shared object all the way down is
the deep version with extra function calls in it, and measuring it proves
nothing (see topic 1's broken-experiment note).

WHAT TO LOOK FOR: the public surface. `dir(app.shallow)` lists what four layers
export; `dir(app.deep)` lists what one module exports. That difference is the
"interface surface" row of the table, and neither number is an opinion.
"""
from .router import router  # noqa: F401
from .service import OrderListing, list_customer_orders  # noqa: F401
from .repository import OrderRecord, fetch_orders, count_orders  # noqa: F401
from .dao import OrderRow, select_orders, select_order_count  # noqa: F401

__all__ = [
    "router",
    "OrderListing", "list_customer_orders",
    "OrderRecord", "fetch_orders", "count_orders",
    "OrderRow", "select_orders", "select_order_count",
]
