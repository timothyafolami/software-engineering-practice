"""Layer 8 Topic 3 - Python: the handler that silently handles only the first failure.

WHAT THIS DEMONSTRATES: `asyncio.TaskGroup` raises an ExceptionGroup (PEP 654)
when several children fail. A plain `except Exception` handler does not match an
ExceptionGroup at all in the general case -- and where it does appear to work,
it collapses N failures into one report. Any endpoint that fans out with
TaskGroup needs `except*`.

Also demonstrated, because it is the other half of the same discipline:
`raise ... from e` sets `__cause__`; raising inside an `except` block preserves
the original as `__context__` anyway; `from None` deliberately erases it, which
is right only when the inner exception is an implementation detail nobody will
ever need.

WHAT TO LOOK FOR: the BROKEN run reports ONE failed dependency out of three. The
FIXED run reports all three, and separates the retryable one from the bug --
which is the difference between "the payment service is down" and "the payment
service is down, inventory timed out, and we have a KeyError in our own code".

    python3 python/exception_groups.py
"""
import asyncio
import traceback


class Unavailable(Exception):
    """Category 2: the same call, unchanged, might succeed later."""

    def __init__(self, dep, retry_after=1.0):
        super().__init__(f"{dep} unavailable")
        self.dep, self.retry_after = dep, retry_after


class NotFound(Exception):
    """Category 1: the caller can do something specific -- return a 404."""


async def payments():
    await asyncio.sleep(0.01)
    raise Unavailable("payments", retry_after=2)


async def inventory():
    await asyncio.sleep(0.01)
    raise Unavailable("inventory", retry_after=5)


async def pricing():
    await asyncio.sleep(0.01)
    # Category 3: a bug in OUR code. Nothing the caller can do. It must not be
    # reported to the user as "a dependency is unavailable".
    table = {"gbp": 1.0}
    return table["eur"]


async def fan_out_BROKEN():
    """The handler almost everyone writes first, and exactly why it misfires.

    `ExceptionGroup` IS a subclass of `Exception`, so this `except` clause runs
    -- which is worse than not running. What it receives is the GROUP, not the
    members, so every `isinstance` check a real handler does comes back False
    and the whole fan-out falls through to the "unknown, must be a bug" branch.

    Three failures, one of them genuinely a bug and two of them retryable with
    known retry-after values, all reported as one 500.
    """
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(payments())
            tg.create_task(inventory())
            tg.create_task(pricing())
    except Exception as exc:
        if isinstance(exc, Unavailable):
            return f"503, Retry-After: {exc.retry_after:.0f}s"
        return (f"500 unknown error: {type(exc).__name__} "
                f"(it caught the group, so no isinstance check could match)")
    return "no failures"


async def fan_out_FIXED():
    """Split by category, which is the only thing the caller can act on.

    Note the shape: `except*` clauses cannot `return`, `break` or `continue`
    (SyntaxError -- the group may match several clauses, so there is no single
    exit). Accumulate into locals and return after the block.
    """
    transient: list[Unavailable] = []
    bugs: list[BaseException] = []
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(payments())
            tg.create_task(inventory())
            tg.create_task(pricing())
    except* Unavailable as eg:
        transient.extend(eg.exceptions)
    except* KeyError as eg:
        bugs.extend(eg.exceptions)
    return transient, bugs


async def main():
    print("=== BROKEN: `except Exception` around a TaskGroup ===")
    print(" ", await fan_out_BROKEN())
    print("  -> two retryable failures with known Retry-After values, and one bug,")
    print("     all reported as one 500. The caller can act on none of it.")

    print("\n=== FIXED: except* per category ===")
    transient, bugs = await fan_out_FIXED()
    for e in transient:
        print(f"  retryable : {e}  (Retry-After: {e.retry_after:.0f}s)")
    for e in bugs:
        print(f"  BUG       : {type(e).__name__}: {e}  <- must 500, not 503")
    print(f"  -> {len(transient)} retryable, {len(bugs)} bug(s). The 503 carries a")
    print("     real Retry-After and the bug is not disguised as a dependency failure.")

    print("\n=== cause chains: from e, implicit context, and from None ===")
    for label, fn in [
        ("raise ... from e", _from_e),
        ("bare raise inside except", _implicit),
        ("raise ... from None", _from_none),
    ]:
        try:
            fn()
        except Exception as exc:
            chain = traceback.format_exception_only(type(exc), exc)[0].strip()
            cause = "__cause__=" + (type(exc.__cause__).__name__ if exc.__cause__ else "None")
            ctx = "__context__=" + (type(exc.__context__).__name__ if exc.__context__ else "None")
            # `from None` does NOT clear __context__ -- it sets __suppress_context__,
            # which stops the traceback printer showing the "During handling of the
            # above exception" section. The original is still attached, so a
            # structured logger that reads __context__ directly will still find it.
            # Worth knowing before relying on `from None` to hide anything.
            sup = f"__suppress_context__={exc.__suppress_context__}"
            print(f"  {label:26s} {cause:20s} {ctx:22s} {sup}")
    print("  -> `from None` suppresses the DISPLAY of the original; it does not detach")
    print("     it. Use it when the inner exception is an implementation detail nobody")
    print("     will ever need -- not as a way to make something disappear.")


def _from_e():
    try:
        {"a": 1}["b"]
    except KeyError as e:
        raise NotFound("order 7 not found") from e


def _implicit():
    try:
        {"a": 1}["b"]
    except KeyError:
        raise NotFound("order 7 not found")


def _from_none():
    try:
        {"a": 1}["b"]
    except KeyError:
        raise NotFound("order 7 not found") from None


if __name__ == "__main__":
    asyncio.run(main())
