"""
Layer 7 · Topic 1 — IDOR as a missing authorization check (Python).

Runs entirely in-process: no DB, no network, one command, no arguments.
It seeds 1,500 invoices (ids 1..1500) across three tenants -- alice, bob,
carol -- INTERLEAVED so enumeration hits all three, then fires the same
1,000-request enumeration (logged in as alice) at three handler variants
and counts, per variant, how many responses were 200 with an owner that is
NOT alice. That count is the leak: rows a different logged-in user walked
off with, per 1,000 requests.

The three variants are the three enforcement layers from the README:

  vulnerable   -- store.get(id): a primary-key lookup that has no idea who
                  is asking. This is db.get(Invoice, id) / findUnique / findById.
  fixed_query  -- store.get_scoped(id, caller): WHERE id=? AND owner_id=?.
                  The wrong-owner row is NEVER fetched; a mismatch is a 404.
  fixed_rls    -- the handler still writes the naive get(), but the DATA
                  LAYER carries the caller and refuses to return rows the
                  caller does not own. This is the in-process analogue of
                  Postgres Row-Level Security: forgetting the filter in the
                  handler now fails CLOSED. (For the real Postgres version,
                  with the SET vs SET LOCAL pooling footgun, see
                  idor_rls_postgres.py.)

What to look for in the output: `vulnerable` leaks ~2/3 of requests (alice
owns 1/3 of the ids, so a uniformly random id belongs to someone else 2/3
of the time); both fixed variants leak exactly 0 and return 404 -- not 403 --
on a wrong-owner id, which is the enumeration-relevant difference.
"""
from dataclasses import dataclass

N_INVOICES = 1500
TENANTS = {1: "alice", 2: "bob", 3: "carol"}
CALLER = 1  # we are logged in as alice for the whole run
N_REQUESTS = 1000


@dataclass(frozen=True)
class Invoice:
    id: int
    owner_id: int
    amount_cents: int


class InvoiceStore:
    """Stands in for the invoices table. The three access methods are the
    three enforcement layers; a handler picks exactly one."""

    def __init__(self):
        # Interleaved ownership: id 1->alice, 2->bob, 3->carol, 4->alice...
        self._rows = {
            i: Invoice(id=i, owner_id=((i - 1) % 3) + 1, amount_cents=i * 100)
            for i in range(1, N_INVOICES + 1)
        }
        self._request_user = None  # the RLS "session variable"

    # --- vulnerable: lookup by primary key, no notion of a caller ---
    def get(self, invoice_id):
        return self._rows.get(invoice_id)

    # --- query-layer fix: the owner filter is part of the fetch ---
    def get_scoped(self, invoice_id, owner_id):
        row = self._rows.get(invoice_id)
        return row if row is not None and row.owner_id == owner_id else None

    # --- data-layer fix (RLS analogue): filter applied below the handler ---
    def set_request_user(self, owner_id):
        self._request_user = owner_id

    def get_rls(self, invoice_id):
        # Same shape as get(), but the policy is enforced here, where the
        # handler author cannot forget it.
        row = self._rows.get(invoice_id)
        if row is None or self._request_user is None:
            return None
        return row if row.owner_id == self._request_user else None


def handler_vulnerable(store, caller, invoice_id):
    row = store.get(invoice_id)                      # <-- trusts the id
    return (200, row) if row else (404, None)


def handler_fixed_query(store, caller, invoice_id):
    row = store.get_scoped(invoice_id, caller)       # WHERE id AND owner_id
    return (200, row) if row else (404, None)


def handler_fixed_rls(store, caller, invoice_id):
    store.set_request_user(caller)                   # SET LOCAL app.current_user
    row = store.get_rls(invoice_id)                  # naive get(), policy below
    return (200, row) if row else (404, None)


def enumeration_ids(n):
    """Deterministic LCG so the run is reproducible and the numbers are
    measured, not chosen. IDs drawn ~uniformly from 1..N_INVOICES."""
    x = 1
    for _ in range(n):
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        yield 1 + (x % N_INVOICES)


def run(handler, label):
    store = InvoiceStore()
    leaked = ok_own = not_found = 0
    for invoice_id in enumeration_ids(N_REQUESTS):
        status, row = handler(store, CALLER, invoice_id)
        if status == 200:
            if row.owner_id != CALLER:
                leaked += 1
            else:
                ok_own += 1
        else:
            not_found += 1
    # Status a KNOWN wrong-owner id returns (id 2 is bob's):
    wrong_status, _ = handler(store, CALLER, 2)
    print(f"  {label:<14} leaked={leaked:>4}/{N_REQUESTS}   "
          f"own={ok_own:>4}  not_found={not_found:>4}   "
          f"wrong-owner id -> HTTP {wrong_status}")
    return leaked


def main():
    print("Layer 7 · Topic 1 — IDOR enumeration (logged in as alice, id=1)")
    print(f"seed: {N_INVOICES} invoices, 3 tenants interleaved, "
          f"alice owns {N_INVOICES // 3} of them")
    print(f"attack: {N_REQUESTS} requests, ids uniform over 1..{N_INVOICES}\n")
    print("  handler        wrong-owner rows leaked per 1,000 requests")
    run(handler_vulnerable, "vulnerable")
    run(handler_fixed_query, "fixed_query")
    run(handler_fixed_rls, "fixed_rls")
    print("\nRead: vulnerable leaks ~667/1000 because a uniform id is not "
          "alice's 2/3 of the time; both fixes leak 0. The fixes return 404, "
          "not 403 -- a 403 would confirm the id EXISTS, still leaking "
          "existence to an enumerator. 404 leaks nothing.")


if __name__ == "__main__":
    main()
