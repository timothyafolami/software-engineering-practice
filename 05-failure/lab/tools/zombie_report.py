"""
Layer 5 · Topic 2 - the zombie report: work completed after nobody was waiting.

WHAT THIS DEMONSTRATES
  Asks every hop in the chain for its own counters and prints them side by
  side. The number in the middle column is the topic:

    zombies   completions that finished AFTER the caller's deadline had
              already passed. Correct answers, delivered to a socket whose
              reader gave up. In the naive variant they are pure waste, and
              they occupied a pool slot the whole time.

    deadline_rejected  requests refused before starting, because the budget
              left was below DEADLINE_SLACK_MS. This is the propagated
              variant's whole mechanism, and the count is the work you did
              NOT do - which is capacity you got back.

WHAT TO LOOK FOR IN THE OUTPUT
  Run it after 02_chain_naive.js and again after 02_chain_deadline.js.
  Between the two, zombies should collapse and deadline_rejected should
  appear, while the gateway's success rate goes UP - the point being that
  refusing work you cannot deliver is not a degradation, it is the fix.

  Watch C's `pool_in_use / pool_total` too. That is the line back to topic 1:
  zombie work is not wasted CPU, it is an occupied count.

RUN
  Inside the stack, from any application container:
      docker compose exec gateway python -m tools.zombie_report

  Or from the host, against the published ports:
      python3 tools/zombie_report.py
      python3 tools/zombie_report.py http://localhost:8001 http://localhost:8003

  With no argument it tries the in-container service names first and falls
  back to the published host ports, so the same command works from both
  sides of the network boundary.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

IN_CONTAINER = ["http://gateway:8000", "http://service-b:8000", "http://service-c:8000"]
FROM_HOST = ["http://localhost:8001", "http://localhost:8002", "http://localhost:8003"]


def fetch(base: str, path: str = "/admin/zombies", timeout: float = 3.0) -> dict | None:
    try:
        with urllib.request.urlopen(base.rstrip("/") + path, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def resolve(argv: list[str]) -> tuple[list[str], list[dict]]:
    if len(argv) > 1:
        bases = argv[1:]
        return bases, [fetch(b) for b in bases]
    for bases in (IN_CONTAINER, FROM_HOST):
        reports = [fetch(b) for b in bases]
        if any(r is not None for r in reports):
            return bases, reports
    return [], []


def main(argv: list[str]) -> int:
    bases, reports = resolve(argv)
    if not bases:
        print("No hop answered on either the in-container names or the published host ports.")
        print("Start the chain first:")
        print("  docker compose --profile chain up -d --build")
        print("Then rerun. From the host the ports are 8001 (gateway), 8002 (b), 8003 (c).")
        return 1

    headers = ["hop", "propagate", "received", "completed", "failed", "zombies",
               "rejected", "abandoned", "timeouts", "retries", "pool", "success %"]
    rows = []
    for base, report in zip(bases, reports):
        if report is None:
            rows.append([base, "-", "unreachable", "", "", "", "", "", "", "", "", ""])
            continue
        received = max(1, int(report["received"]))
        rows.append([
            str(report.get("role", base)),
            "on" if report.get("propagate_deadline") else "off",
            str(report["received"]), str(report["completed"]), str(report["failed"]),
            str(report["zombies"]), str(report["deadline_rejected"]),
            str(report.get("deadline_abandoned", 0)),
            str(report["timeouts"]), str(report["retries"]),
            f"{report['pool_in_use']}/{report['pool_total']}",
            f"{100.0 * int(report['completed']) / received:.1f}",
        ])

    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    print()
    print("  ".join(h.rjust(widths[i]) for i, h in enumerate(headers)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(str(c).rjust(widths[i]) for i, c in enumerate(row)))

    leaf = next((r for r in reports if r and r.get("role") == "service_c"), None)
    gateway = next((r for r in reports if r and r.get("role") == "gateway"), None)
    if leaf:
        seconds = max(1.0, float(leaf["uptime_s"]))
        variant = "propagated" if leaf.get("propagate_deadline") else "naive"
        line = (f"\n{variant:<11} zombie completions/s = {int(leaf['zombies']) / seconds:.2f}"
                f"   C pool in use = {leaf['pool_in_use']}/{leaf['pool_total']}")
        if gateway:
            received = max(1, int(gateway["received"]))
            line += f"   gateway success = {100.0 * int(gateway['completed']) / received:.1f}%"
        print(line)
        if not leaf.get("propagate_deadline") and int(leaf["zombies"]) > 0:
            print("\nEvery one of those completions was correct, complete, and useless. The")
            print("gateway had already returned 504. What they cost was pool slots, which is")
            print("why a dependency that is SLOW is worse than one that is down.")
        if leaf.get("propagate_deadline"):
            refused = int(leaf["deadline_rejected"])
            abandoned = int(leaf.get("deadline_abandoned", 0))
            print(f"\n{refused} requests were refused on arrival and {abandoned} were abandoned")
            print(f"after the pool wait, both because less than {leaf['deadline_slack_ms']}ms of budget")
            print("remained. That is the work this variant did not do, and the reason it has")
            print("capacity the other one does not.")
            print("\nThe second number is usually the larger one, and it is the one a deadline")
            print("checked only on arrival cannot produce: under load the queue wait IS the")
            print("budget, so a request that was affordable when it arrived is not affordable")
            print("by the time a connection is free.")
    print("\nCounters are per process and reset with the process. Run this straight after")
    print("a k6 run, not after a restart.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
