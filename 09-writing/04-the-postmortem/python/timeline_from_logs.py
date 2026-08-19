"""
Layer 9 Topic 4 -- assemble the skeleton of a postmortem timeline from log
timestamps, without inventing anything.

WHAT THIS DEMONSTRATES: the machine half of a postmortem. Timeline assembly is
now tooling work (see the topic README on what the vendors automate); the
contributing-factor analysis and the detection-gap story are not. This script
does the machine half honestly: it groups log lines into distinct message
shapes, reports when each shape was FIRST and LAST seen and how many times, and
emits markdown rows you can paste under `## Timeline`.

What it deliberately does NOT do:
  * It does not normalise or convert timestamps. They are reproduced exactly as
    they appeared, because a timezone you assumed is a number you did not
    measure. Check they are UTC yourself.
  * It does not fill the third column. Only you know how you knew -- the script
    can tell you which file and line it read, and that is what it writes.
  * It does not guess when anything STARTED. It prints the earliest line it saw
    and says plainly that the true start is unknown if the incident predates it.
    That refusal is the point: the gap between the log window and the incident
    window is a detection gap, and it belongs in the document as `unknown`.

WHAT TO LOOK FOR IN THE OUTPUT: the COVERAGE block first. If your log window
starts after the change that made the incident possible, the timeline you are
about to write cannot start where the README says it must, and the first row of
your table is `unknown` with a retention note.

  docker compose logs --no-color --since 72h api | python3 python/timeline_from_logs.py
  python3 python/timeline_from_logs.py /path/to/app.log /path/to/postgres.log
  python3 python/timeline_from_logs.py            # no input: SYNTHETIC demo of the parser
"""
from __future__ import annotations

import re
import select
import sys
from pathlib import Path

# Seconds to wait for piped input before deciding there is none. Long enough for
# a shell pipeline to deliver its first bytes, short enough not to look hung.
STDIN_WAIT = 1.5

# --- timestamp recognisers -------------------------------------------------
# Each entry: (name, compiled regex, key function producing a sortable tuple).
# The matched text is emitted verbatim; the key exists only for ordering.
MONTHS = {m: i for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


def _iso_key(m: re.Match) -> tuple:
    d = m.group(0).replace("T", " ")
    return (1, re.sub(r"[^0-9]", "", d)[:20].ljust(20, "0"))


def _clf_key(m: re.Match) -> tuple:
    day, mon, rest = m.group("day"), m.group("mon"), m.group("time")
    year = m.group("year")
    return (1, f"{year}{MONTHS.get(mon, 0):02d}{int(day):02d}" + re.sub(r"[^0-9]", "", rest))


def _syslog_key(m: re.Match) -> tuple:
    # No year in the line. Sorting stays within the file; ordering across a
    # year boundary is not recoverable and the script says so rather than
    # picking a year.
    mon, day, t = m.group("mon"), m.group("day"), m.group("time")
    return (0, f"{MONTHS.get(mon, 0):02d}{int(day):02d}" + re.sub(r"[^0-9]", "", t))


RECOGNISERS = [
    ("iso8601",
     re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
     _iso_key),
    ("common-log",
     re.compile(r"\[(?P<day>\d{2})/(?P<mon>[A-Z][a-z]{2})/(?P<year>\d{4}):(?P<time>\d{2}:\d{2}:\d{2})\s*(?:[+-]\d{4})?\]"),
     _clf_key),
    ("syslog",
     re.compile(r"(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<time>\d{2}:\d{2}:\d{2})"),
     _syslog_key),
]

# docker compose prefixes every line with "service-1  | "
COMPOSE_PREFIX = re.compile(r"^[A-Za-z0-9_.-]+\s*\|\s")

# Shape normalisation: strip the varying parts so repeated events collapse.
SHAPE_RULES = [
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"\b[0-9a-fA-F]{7,40}\b"), "<hex>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
    (re.compile(r"\d+(?:\.\d+)?(?:ms|s\b)"), "<duration>"),
    (re.compile(r"\b\d+\b"), "<n>"),
    (re.compile(r"\s+"), " "),
]

DEMO = """\
2026-01-01T00:00:00.000Z INFO  uvicorn running on http://0.0.0.0:8000
2026-01-01T00:00:03.114Z INFO  POST /checkout 200 in 121ms
2026-01-01T00:00:04.882Z INFO  POST /checkout 200 in 118ms
2026-01-01T00:11:52.004Z WARN  pricing call took 3182ms for order 99120
2026-01-01T00:11:52.180Z INFO  POST /checkout 200 in 3204ms
2026-01-01T00:12:31.771Z WARN  pricing call took 4410ms for order 99131
2026-01-01T00:12:36.002Z INFO  POST /checkout 200 in 4433ms
2026-01-01T00:14:10.559Z ERROR client disconnected before response, path=/checkout
2026-01-01T00:41:02.300Z INFO  POST /orders 200 in 44ms
"""


def shape(text: str) -> str:
    s = text.strip()
    for pattern, repl in SHAPE_RULES:
        s = pattern.sub(repl, s)
    return s[:96]


def parse(lines, origin: str, events: dict, stats: dict) -> None:
    for lineno, raw in enumerate(lines, 1):
        line = raw.rstrip("\n")
        stats["lines"] += 1
        body = COMPOSE_PREFIX.sub("", line)
        stamp = key = None
        for name, rx, keyfn in RECOGNISERS:
            m = rx.search(body)
            if m:
                stamp, key = m.group(0), keyfn(m)
                stats["formats"].add(name)
                rest = (body[:m.start()] + body[m.end():])
                break
        if stamp is None:
            stats["untimestamped"] += 1
            continue
        sh = shape(rest)
        if not sh:
            continue
        ev = events.setdefault(sh, {
            "first_key": key, "first": stamp, "last_key": key, "last": stamp,
            "count": 0, "where": f"{origin}:{lineno}",
        })
        ev["count"] += 1
        if key < ev["first_key"]:
            ev["first_key"], ev["first"], ev["where"] = key, stamp, f"{origin}:{lineno}"
        if key > ev["last_key"]:
            ev["last_key"], ev["last"] = key, stamp


def report(events: dict, stats: dict, demo: bool) -> None:
    if demo:
        print("!" * 72)
        print("!! SYNTHETIC DEMO INPUT. These lines were made up to show the parser.")
        print("!! They are not measurements and must never reach a document.")
        print("!! Pipe your own logs in:  docker compose logs api | python3 ...")
        print("!" * 72)
        print()

    if not events:
        print("No timestamped lines recognised.")
        print(f"  lines read: {stats['lines']}, none carrying a recognised timestamp.")
        print("  Recognised formats: ISO-8601, common-log ([18/Aug/2026:02:10:11]), syslog.")
        print("  If your logs use something else, that is worth a line in the timeline's")
        print("  third column: the format cost you time during the incident.")
        return

    ordered = sorted(events.items(), key=lambda kv: kv[1]["first_key"])
    # Year-less timestamps (syslog) cannot be ordered against dated ones without
    # assuming a year, so they are reported separately rather than interleaved.
    dated = [kv for kv in ordered if kv[1]["first_key"][0] == 1]
    undated = [kv for kv in ordered if kv[1]["first_key"][0] == 0]
    ranked = dated or undated
    earliest = ranked[0][1]
    latest = max((e for _, e in ranked), key=lambda e: e["last_key"])

    print("COVERAGE -- read this before the table")
    print(f"  lines read           : {stats['lines']}")
    print(f"  with a timestamp     : {stats['lines'] - stats['untimestamped']}")
    print(f"  distinct event shapes: {len(events)}")
    print(f"  formats seen         : {', '.join(sorted(stats['formats'])) or 'none'}")
    print(f"  earliest line        : {earliest['first']}")
    print(f"  latest line          : {latest['last']}")
    print()
    print("  This window is what you have, not what happened. If the change that made")
    print("  the incident possible is older than the earliest line above, your first")
    print("  timeline row is `unknown` with a retention note -- and that row is a")
    print("  detection gap, which is a finding rather than a hole.")
    print()
    print("  Timestamps below are reproduced verbatim. No timezone conversion was")
    print("  performed. The template says UTC; confirm these are before you paste.")
    print()

    print("| Time | What happened | How we knew (or didn't) |")
    print("|---|---|---|")
    for sh, ev in (dated or undated):
        repeat = "" if ev["count"] == 1 else f" (x{ev['count']}, last at {ev['last']})"
        print(f"| {ev['first']} | {sh}{repeat} | {ev['where']} — `<what this told you at the time, or 'nobody was looking'>` |")
    if dated and undated:
        print()
        print("YEAR-LESS TIMESTAMPS -- ordered separately, because placing them against")
        print("the dated rows above would mean assuming a year:")
        for sh, ev in undated:
            repeat = "" if ev["count"] == 1 else f" (x{ev['count']}, last at {ev['last']})"
            print(f"| {ev['first']} | {sh}{repeat} | {ev['where']} |")
    print()
    print("NEXT, BY HAND -- the part the machine cannot do:")
    print("  * Delete every row that is not an observable event in the incident.")
    print("    A generated timeline is over-complete; a useful one is edited.")
    print("  * Add the rows that are NOT in any log: the deploy that made it possible,")
    print("    the first support ticket, the moment a human formed a hypothesis.")
    print("  * Fill the third column with what you actually knew at the time. A row")
    print("    whose third column you cannot fill is telling you something.")


def stdin_has_data() -> bool:
    """True if something is piped in and has started arriving.

    A terminal is never treated as input. A pipe that produces nothing within
    STDIN_WAIT is treated as no input, so the script runs its demo instead of
    hanging with no output -- and it says which it did.
    """
    if sys.stdin.isatty():
        return False
    try:
        ready, _, _ = select.select([sys.stdin], [], [], STDIN_WAIT)
    except (OSError, ValueError):
        return False
    if ready:
        return True
    print(f"(nothing on stdin after {STDIN_WAIT}s -- running the synthetic demo below.", file=sys.stderr)
    print(" pipe logs in, or pass file paths, for real output)", file=sys.stderr)
    return False


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    events: dict = {}
    stats = {"lines": 0, "untimestamped": 0, "formats": set()}
    demo = False

    if args:
        for a in args:
            p = Path(a)
            if not p.is_file():
                print(f"skip (not a file): {p}", file=sys.stderr)
                continue
            with p.open(errors="replace") as fh:
                parse(fh, p.name, events, stats)
    elif stdin_has_data():
        parse(sys.stdin, "stdin", events, stats)

    if not args and stats["lines"] == 0:
        # Nothing was piped in (or the pipe was empty). Show what the parser does
        # rather than an empty report, and label it so it can never be mistaken
        # for data.
        demo = True
        parse(DEMO.splitlines(), "DEMO", events, stats)

    report(events, stats, demo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
