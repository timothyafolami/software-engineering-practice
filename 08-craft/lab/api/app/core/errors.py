"""Topic 3: the error taxonomy, and the one place it becomes HTTP.

WHAT THIS DEMONSTRATES: three categories, one base class, and exactly one
translation to a transport. Nothing below the routing layer is allowed to know
that HTTP exists -- raising `HTTPException` from a repository couples your data
access to a protocol it should never have heard of.

WHAT TO LOOK FOR: `install_error_handlers()` is the ONLY code in this app that
turns an exception into a status code. Category 3 (bugs) deliberately has no
handler: it falls through to Starlette's 500, which is the loud, discoverable
outcome the topic argues for.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("craft.errors")


class AppError(Exception):
    """Base for every error this application raises on purpose.

    Anything that is NOT an AppError is category 3 -- a bug -- and must reach
    the top of the stack unmodified so it shows up as a 500 with a traceback.
    """

    status_code = 500
    code = "app_error"

    def __init__(self, message: str, /, **detail):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def as_body(self) -> dict:
        return {"error": self.code, "message": self.message, **self.detail}


# --- category 1: caller-actionable ------------------------------------------
# The caller can do something specific and different. These belong in the
# signature, in the docstring, and in the OpenAPI `responses` block -- an error
# the caller is supposed to handle but cannot see is a rumour, not a contract.


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class Invalid(AppError):
    status_code = 422
    code = "invalid"


# --- category 2: retryable / transient --------------------------------------
# Distinguishing property, stated precisely: the same call, unchanged, might
# succeed later. `retry_after` is the only thing that makes this actionable
# rather than decorative.


class Unavailable(AppError):
    status_code = 503
    code = "unavailable"

    def __init__(self, message: str = "dependency unavailable", /, *, retry_after: float = 1.0):
        super().__init__(message, retry_after=retry_after)
        self.retry_after = retry_after


class DeadlineExceeded(AppError):
    """The request ran out of its budget. Topic 7's fix kit raises this."""

    status_code = 504
    code = "deadline_exceeded"


class Shed(AppError):
    """Load shedding: refused before starting, because we could not finish in time."""

    status_code = 503
    code = "shed"


# --- category 3: bugs -------------------------------------------------------
# There is no class here, and that is the design. A KeyError on a dict you
# built, a TypeError, a violated invariant -- nothing the caller can do, so
# crash loudly. A 500 with a stack trace in your logs is strictly better than
# a 200 with wrong data, because only one of them is discoverable.


def install_error_handlers(app) -> None:
    """Map the taxonomy to HTTP exactly once, at the edge.

    Import is local so that `core.errors` stays importable (and testable, and
    mutable by mutmut) without FastAPI installed -- topics 5, 8 and 9 use this
    module natively on macOS with no container.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse

    @app.exception_handler(AppError)
    async def _app_error(request: "Request", exc: AppError):  # noqa: F821
        headers = {}
        if isinstance(exc, Unavailable):
            # A 503 without Retry-After tells the caller to back off by an
            # amount it has to guess. Guessing is how you get a thundering
            # herd; see Layer 5.
            headers["Retry-After"] = str(int(max(1, round(exc.retry_after))))
        logger.info("app_error", extra={"code": exc.code, "path": request.url.path})
        return JSONResponse(status_code=exc.status_code, content=exc.as_body(), headers=headers)
