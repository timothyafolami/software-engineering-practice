#!/usr/bin/env python3
"""Topic 6: turn a generated artifact into an actual contract, in one command.

WHAT THIS DEMONSTRATES: FastAPI derives the OpenAPI schema from the route
signatures, so the live spec CANNOT disagree with the code -- a breaking change
rewrites the contract at the same instant, and schemathesis then dutifully
verifies the new contract against the new code and reports success. Committing
the schema is the one line of discipline that fixes that.

WHAT TO LOOK FOR:
    python snapshot_openapi.py            # write openapi.snapshot.json
    python snapshot_openapi.py --check    # exit 1 if the live schema drifted

`--check` is the CI step. `oasdiff breaking` (topic 6's How to run) is the
smarter version that only fails on breaking diffs; this one fails on any diff
and is the thirty-second version you can add today.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

SNAPSHOT = pathlib.Path(__file__).with_name("openapi.snapshot.json")


def live_schema() -> dict:
    from app.main import app

    return app.openapi()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="fail if live differs from snapshot")
    args = ap.parse_args()

    live = json.dumps(live_schema(), indent=2, sort_keys=True) + "\n"

    if not args.check:
        SNAPSHOT.write_text(live)
        print(f"wrote {SNAPSHOT} ({len(live)} bytes)")
        return 0

    if not SNAPSHOT.exists():
        print(f"no snapshot at {SNAPSHOT}; run without --check first", file=sys.stderr)
        return 1
    committed = SNAPSHOT.read_text()
    if committed == live:
        print("openapi.snapshot.json matches the live schema")
        return 0

    import difflib

    sys.stderr.writelines(
        difflib.unified_diff(
            committed.splitlines(keepends=True), live.splitlines(keepends=True),
            fromfile="openapi.snapshot.json", tofile="live /openapi.json",
        )
    )
    print("\nSCHEMA DRIFT: the committed contract and the code disagree.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
