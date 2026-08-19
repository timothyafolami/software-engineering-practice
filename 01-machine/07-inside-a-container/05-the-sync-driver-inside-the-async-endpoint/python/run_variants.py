"""
7.5 -- all four variants in sequence, with cpu.stat paired to every row.

WHAT THIS DEMONSTRATES
  Two independent stalls that produce the same symptom, measured together
  so you can see them come apart:

    Stall A -- the event loop. A synchronous call inside `async def` stops
               every other in-flight request on that worker.
    Stall B -- the cgroup. Every runnable thread in the container drains
               the same quota bucket; when it empties the kernel dequeues
               all of them, including threads only waiting on a socket.

  Both give p50 fine, p99 terrible, no errors, healthy-looking average CPU.
  Exactly one reading tells them apart: nr_throttled in cpu.stat. This
  script therefore refuses to report a p99 without the throttle ratio
  beside it -- a p99 alone is the thing that gets misdiagnosed.

  The pairing is the whole point: the variant with the worst p99 and the
  variant with the highest throttle ratio NEED NOT BE THE SAME VARIANT, and
  understanding why is understanding the topic.

  The interaction, stated once: Starlette's fix for Stall A is to run
  blocking work on a thread pool, and threads are the input to Stall B. So
  every mitigation for the event loop increases the number of runnable
  threads in the cgroup. Variant 3 exists to find where that stops paying.

WHAT TO LOOK FOR IN THE OUTPUT
  1. Variant 1 vs 4: the event-loop stall, in p99, at a near-zero throttle
     ratio. That gap is Stall A with Stall B held out.
  2. Variant 2 vs 3: raising the anyio limiter from 40 to 100 buys
     concurrency and costs quota. Watch the thread census AND the throttle
     ratio move together while p99 does whatever it does.
  3. The `stall` column, which names the diagnosis rather than leaving you
     to infer it: it is computed from the throttle ratio, not from the p99.

RUN
    python3 run_variants.py                       # all four
    python3 run_variants.py --rate 160            # push harder
    python3 run_variants.py --only 1,4            # the event-loop half only

  Needs a running Docker daemon and the harness stack. The throttling half
  of this experiment does not exist on macOS. The event-loop half (variants
  1 vs 4) IS visible on the host against a local Postgres, and it is worth
  seeing there first precisely because the throttle ratio is then
  guaranteed absent -- any p99 you see is Stall A, with no possibility of
  Stall B contaminating it. This script says so and stops rather than
  producing a table of zeros.
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

TOPIC = Path(__file__).resolve().parents[2]
HARNESS = TOPIC / "00-harness"
VARIANTS_PY = Path(__file__).resolve().parents[1] / "app" / "variants.py"

sys.path.insert(0, str(HARNESS / "local"))
from openloop import table  # noqa: E402

DESCRIPTIONS = {
    1: "async def + psycopg2",
    2: "def + psycopg2 (40 tokens)",
    3: "def + psycopg2 (100 tokens)",
    4: "async def + asyncpg",
}


def docker_up() -> bool:
    return shutil.which("docker") is not None and \
        subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def compose(*args: str, env: dict | None = None) -> str:
    merged = {**os.environ, **(env or {})}
    result = subprocess.run(["docker", "compose", *args], cwd=HARNESS,
                            capture_output=True, text=True, env=merged)
    return (result.stdout or "") + (result.stderr or "")


def variant_info() -> dict:
    """Read /variant-info from inside the container.

    From inside, deliberately: curling the published port from the host
    works too, but doing it in-container is the habit -- the cgroup
    readings in that response are the container's own, and asking the
    container for them is how you avoid ever reading the host's by mistake.
    """
    out = compose("exec", "-T", "api", "python", "-c",
                  "import urllib.request,sys;"
                  "sys.stdout.write(urllib.request.urlopen("
                  "'http://127.0.0.1:8000/variant-info').read().decode())")
    match = re.search(r"\{.*\}", out, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


def run_k6(rate: int, duration: str) -> dict:
    # --no-deps: k6 depends_on api, so without it `compose run` re-creates the
    # api container immediately before the measurement -- which for THIS script
    # means the variant under test is replaced by whatever the environment
    # resolves to, and all four rows measure the same handler.
    out = compose("--profile", "load", "run", "--rm", "--no-deps",
                  "-e", "ENDPOINT=/db", "-e", f"RATE={rate}",
                  "-e", f"DURATION={duration}", "k6", "run", "/scripts/steady.js")

    def find(pattern: str, default: float = float("nan")) -> float:
        match = re.search(pattern, out, re.MULTILINE)
        return float(match.group(1)) if match else default

    # steady.js defines handleSummary, which REPLACES k6's built-in summary
    # rather than adding to it, so `med=` / `p(95)=` / `http_reqs .../s` are
    # not in this output. Parse what the script prints; fall back to k6's own
    # shape for an older copy of steady.js.
    seconds = float(re.sub(r"[^\d.]", "", duration) or "0")
    completed = find(r"^completed\s+(\d+)")
    p50 = find(r"^p50\s+([\d.]+) ms")
    if p50 != p50:
        p50 = find(r"med=([\d.]+)ms")
    p99 = find(r"^p99\s+([\d.]+) ms")
    if p99 != p99:
        p99 = find(r"p\(99\)=([\d.]+)ms")
    dropped = find(r"^dropped_iterations\s+(\d+)", float("nan"))
    if dropped != dropped:
        dropped = find(r"dropped_iterations.*?(\d+)", 0.0)

    return {
        "p50": p50,
        # steady.js does not print p95; p99 is the column this experiment
        # reads anyway. Reporting a p95 we did not measure would be worse
        # than an honest gap.
        "p95": find(r"p\(95\)=([\d.]+)ms"),
        "p99": p99,
        "rps": (completed / seconds) if (completed == completed and seconds) else
               find(r"http_reqs.*?([\d.]+)/s"),
        "dropped": dropped,
        "failed": find(r"http_req_failed.*?([\d.]+)%", 0.0),
    }


def start_variant(variant: int, tokens: int, quota: float, db_sleep: float) -> None:
    """Bring the api up with variants.py mounted over the harness app.

    The container spec is byte-identical across all four runs. Only VARIANT
    changes -- and for variant 3, ANYIO_THREAD_TOKENS. If the spec moved
    between rows, the throttle-ratio column would compare two different
    containers and mean nothing.
    """
    env = {
        "VARIANT": str(variant),
        "ANYIO_THREAD_TOKENS": str(tokens),
        "API_CPUS": f"{quota}",
        "WORKERS": "1",
        "DB_SLEEP_S": f"{db_sleep}",
        # Mount the variant module over the image's app and point uvicorn at
        # it. COMPOSE_ env vars cannot express a volume, so this goes through
        # an override file written next to the harness.
    }
    override = HARNESS / "docker-compose.override.yml"
    # Compose auto-loads docker-compose.override.yml for EVERY command run in
    # that directory, so leaving it behind silently re-points the api service
    # for every other experiment in this topic. Remove it on the way out even
    # if this script dies.
    atexit.register(lambda: override.unlink(missing_ok=True))
    override.write_text(f"""# Written by 7.5's run_variants.py. Safe to delete.
#
# Mounts the four handler bodies over the harness app and points uvicorn at
# them, without touching docker-compose.yml -- so the container spec stays
# byte-identical to every other experiment in this topic.
services:
  api:
    volumes:
      # /srv, not /app: the harness image's WORKDIR is /srv and that is where
      # cgroup.py sits, which variants.py imports. Mounted at /app, uvicorn
      # cannot import the module at all and the container dies at startup.
      - {VARIANTS_PY}:/srv/variants.py:ro
    environment:
      VARIANT: "{variant}"
      ANYIO_THREAD_TOKENS: "{tokens}"
      DB_SLEEP_S: "{db_sleep}"
    command: >
      uvicorn variants:app --host 0.0.0.0 --port 8000 --workers 1
""")
    compose("up", "-d", "--force-recreate", "api", env=env)
    # Wait for readiness rather than sleeping a fixed amount: the psycopg2
    # pool opens POOL_MAX connections at startup and that is not instant.
    deadline = time.time() + 60
    while time.time() < deadline:
        if '"ok"' in compose("exec", "-T", "api", "python", "-c",
                             "import urllib.request,sys;"
                             "sys.stdout.write(urllib.request.urlopen("
                             "'http://127.0.0.1:8000/healthz').read().decode())"):
            return
        time.sleep(2)
    raise SystemExit(f"variant {variant} never became healthy -- check "
                     f"`docker compose logs api` from {HARNESS}")


def main() -> None:
    parser = argparse.ArgumentParser(description="7.5 -- four variants, one spec")
    parser.add_argument("--rate", type=int, default=120,
                        help="k6 arrival rate, req/s (default 120)")
    parser.add_argument("--duration", default="45s")
    parser.add_argument("--quota", type=float, default=1.0, help="API_CPUS")
    parser.add_argument("--db-sleep", type=float, default=0.050,
                        help="pg_sleep seconds inside the query (default 0.050)")
    parser.add_argument("--tokens", type=int, default=100,
                        help="the raised anyio limiter for variant 3")
    parser.add_argument("--only", default="1,2,3,4")
    args = parser.parse_args()

    print("7.5 -- the sync driver inside the async endpoint, under a quota")
    print()

    if not docker_up():
        print("  docker daemon is not running.")
        print()
        print("  The throttling half of this experiment does not exist on macOS:")
        print("  Darwin has no cgroupfs, so there is no cpu.max to set and no")
        print("  nr_throttled to read, and a table of zeros would be a lie.")
        print()
        print("  The EVENT-LOOP half is visible on the host, and it is worth seeing")
        print("  there first -- because with no cgroup, the throttle ratio is")
        print("  guaranteed to be zero, so any p99 you measure is Stall A with no")
        print("  possibility of Stall B contaminating it:")
        print()
        print("    # with a local Postgres running (check `pg_isready` first)")
        print("    export DATABASE_URL=postgresql://lab:lab@localhost:5432/container_lab")
        print("    VARIANT=1 uvicorn variants:app --port 8000 --workers 1  # the bug")
        print("    VARIANT=4 uvicorn variants:app --port 8000 --workers 1  # the fix")
        print("    # then drive it open-loop from the harness's stdlib driver:")
        print("    python3 ../../00-harness/local/openloop.py")
        print()
        print("  Start Docker Desktop for the full four-variant table.")
        raise SystemExit(1)

    wanted = [int(v) for v in args.only.split(",")]
    print(f"  /db at {args.rate} req/s for {args.duration}, "
          f"cpus {args.quota}, 1 worker, pg_sleep {args.db_sleep}s")
    print("  Every row runs the SAME container spec. Only the handler changes.")
    print()

    rows = []
    for variant in wanted:
        tokens = args.tokens if variant == 3 else 40
        start_variant(variant, tokens, args.quota, args.db_sleep)

        before = variant_info()
        db_sleep = args.db_sleep
        result = run_k6(args.rate, args.duration)
        after = variant_info()

        stat_before = before.get("cpu_stat") or {}
        stat_after = after.get("cpu_stat") or {}
        d_periods = stat_after.get("nr_periods", 0) - stat_before.get("nr_periods", 0)
        d_throttled = stat_after.get("nr_throttled", 0) - stat_before.get("nr_throttled", 0)
        ratio = d_throttled / d_periods if d_periods else float("nan")

        # Both halves of the diagnosis are MEASURED. This column used to
        # decide stall A from the variant number -- variants 1, 2 and 3 were
        # labelled "A (event loop)" whatever they did -- which is not a
        # diagnosis, it is the row's own name written twice. Measured, the
        # three behave completely differently: 2 and 3 hand the blocking call
        # to the anyio thread pool and come back at p50 52ms against a 50ms
        # pg_sleep, while 1 blocks the loop and comes back at 4190ms.
        #
        # Stall A shows up as a p50 far above the floor the query itself sets:
        # a handler that is not blocking the loop cannot beat DB_SLEEP_S and
        # should not be far above it. Stall B is the throttle ratio, which is
        # independent of A -- that independence is the whole sub-topic.
        floor_ms = db_sleep * 1000.0
        a_present = result["p50"] == result["p50"] and result["p50"] > 2.5 * floor_ms
        b_present = d_periods > 0 and ratio >= 0.05
        if d_periods == 0 and not a_present:
            stall = "no cgroup data"
        elif a_present and b_present:
            stall = "A + B"
        elif a_present:
            stall = "A (event loop)"
        elif b_present:
            stall = "B (cgroup)"
        else:
            stall = "neither"

        rows.append([
            f"{variant} {DESCRIPTIONS[variant]}",
            f"{result['p50']:.0f}", f"{result['p99']:.0f}", f"{result['rps']:.0f}",
            f"{ratio:.3f}" if d_periods else "n/a",
            str(after.get("os_threads", "?")),
            str(after.get("anyio_total_tokens", "?")),
            f"{result['dropped']:.0f}",
            stall,
        ])
        print(f"  ran: variant {variant} -- {DESCRIPTIONS[variant]}")

    print()
    print(table(rows, ["variant", "p50 ms", "p99 ms", "req/s", "throttle ratio",
                       "OS threads", "tokens", "dropped", "which stall"]))
    print()
    print("  Read the p99 column and the throttle-ratio column as two separate")
    print("  measurements that happen to be printed next to each other. If the")
    print("  worst p99 and the highest ratio are different rows, you have the")
    print("  result this sub-topic exists for: two mechanisms, one symptom.")
    print()
    print("  Sanity checks before believing any of it:")
    print("   * variant 1 not dramatically worse than variant 4 -> the query")
    print("     returns too fast to block measurably. Raise --db-sleep.")
    print("   * variants 2 and 3 identical -> you never exceeded 40 requests in")
    print("     flight, so the limiter was never the constraint. Raise --rate.")
    print("   * every ratio zero -> the quota is not binding at this rate. That")
    print("     is a valid p99 result, but the INTERACTION is not in your data:")
    print("     lower --quota or raise --rate.")
    print("   * dropped not ~0 -> k6 ran out of VUs and every latency above is")
    print("     understated. You are measuring the load generator.")
    print()
    print("  Clean up the override this script wrote:")
    print(f"    rm {HARNESS / 'docker-compose.override.yml'}")


if __name__ == "__main__":
    main()
