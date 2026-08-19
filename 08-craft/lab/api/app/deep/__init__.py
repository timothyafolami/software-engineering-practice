"""Topic 1: the same feature as `app.shallow`, as one deep module.

WHAT THIS DEMONSTRATES: depth is `functionality provided / interface surface`,
so the deep version is NOT the one with the biggest body -- it is the one whose
public surface is small relative to what sits behind it. Compare
`dir(app.deep)` with `dir(app.shallow)`.

The body here is not one 200-line function either. It has private helpers, so
it has seams; they are just not interface surface. That distinction is the
second broken-experiment note in topic 1 and it is easy to get wrong in the
"fix" direction.

WHAT TO LOOK FOR: this package exports three names -- one function, the type
it returns, and the router that mounts it. The shallow package exports nine,
across four files, three of which are structurally identical DTOs. Count them
yourself rather than trusting this sentence:
`python3 -c "import app.deep as m; print(len(m.__all__), sorted(m.__all__))"`.
"""
from .orders import OrderPage, customer_order_page, router  # noqa: F401

__all__ = ["router", "customer_order_page", "OrderPage"]
