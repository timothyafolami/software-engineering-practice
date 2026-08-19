"""Topic 6: the only test in this repository that is actually a contract test.

WHAT THIS DEMONSTRATES: FastAPI DERIVES the schema from the route signatures, so
a breaking change to the code rewrites the contract at the same instant. Running
schemathesis against the live `/openapi.json` therefore validates internal
consistency and nothing else -- the tool verifies the new contract against the
new code and reports success.

The committed snapshot is the third independent thing a contract test needs: the
provider's behaviour, the consumer's expectation, and a written contract that
outlives both. Any scheme with only two of the three checks that something
equals itself.

WHAT TO LOOK FOR: `test_live_schema_matches_the_committed_snapshot` is the CI
gate. Make topic 6's `total: int -> str` change and watch this fail while
schemathesis against the live schema stays green. That null result IS the
finding.

    pytest tests/contract -q
"""
from __future__ import annotations

import json
import pathlib

import pytest

SNAPSHOT = pathlib.Path(__file__).resolve().parents[2] / "openapi.snapshot.json"


@pytest.fixture(scope="module")
def live():
    from app.main import app

    return app.openapi()


@pytest.fixture(scope="module")
def committed():
    if not SNAPSHOT.exists():
        pytest.fail(f"no committed contract at {SNAPSHOT}; run `make snapshot`")
    return json.loads(SNAPSHOT.read_text())


def test_live_schema_matches_the_committed_snapshot(live, committed):
    """The gate. Any drift fails the build until someone commits the new contract."""
    assert live == committed, (
        "the live schema and openapi.snapshot.json disagree. If the change is "
        "intended, run `make snapshot` and commit it -- deliberately, in the "
        "same PR, so the contract change is reviewable rather than incidental."
    )


def test_every_declared_error_response_is_in_the_schema(live):
    """A 404 that is not in the schema is a contract violation no tool can check.

    Schema-first tooling only knows what the spec says. An error the caller is
    supposed to handle but cannot see in the contract is a rumour, not an
    interface -- which is topic 3's rule, enforced here mechanically.
    """
    missing = []
    for path, ops in live["paths"].items():
        for method, op in ops.items():
            codes = set(op.get("responses", {}))
            if method == "get" and "{" in path and "404" not in codes:
                missing.append(f"{method.upper()} {path}")
    assert not missing, (
        f"path-parameterised GETs with no declared 404: {missing}. Either the "
        f"operation genuinely cannot 404, or the contract is lying."
    )


def test_the_create_operation_declares_links_so_stateful_testing_can_chain(live):
    """Without links there is nothing to chain, and schemathesis's stateful
    phase reports `Missing Open API links` -- a finding about the schema, not a
    tool failure."""
    created = live["paths"]["/orders"]["post"]["responses"]["201"]
    links = created.get("links", {})
    assert links, "POST /orders declares no links; the stateful phase has nothing to follow"
    assert {"GetOrderById", "DeleteOrderById"} <= set(links)
    assert links["GetOrderById"]["parameters"]["order_id"] == "$response.body#/id"


def test_delete_is_idempotent_in_the_contract(live):
    """204 whether or not the row existed: the error defined out of existence.

    If a 404 ever appears here, every consumer acquires a branch it did not have
    before, forever -- and the stateful phase's delete-then-delete step becomes
    a race rather than a defined sequence.
    """
    codes = set(live["paths"]["/orders/{order_id}"]["delete"]["responses"])
    assert "404" not in codes, "DELETE grew a 404; that is a breaking change for every caller"
    assert "204" in codes
