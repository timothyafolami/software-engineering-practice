#!/usr/bin/env python3
"""
Layer 9 lab -- the sanitisation gate, mechanised as far as it honestly can be.

WHAT THIS DEMONSTRATES: the half of lab/README.md's sanitisation checklist that a
regex can actually check (internal hostnames, private IPs, email addresses,
ticket keys, absolute currency figures, plus whatever patterns you put in
sensitive-patterns.txt), separated loudly from the half it cannot (employee
names, customer traffic shapes, employer sign-off).

Run it BEFORE you write, on the draft as it grows -- laundering a finished draft
is much harder than writing a clean one, and the failure mode of laundering is
removing the identifying detail while keeping the shape it was load-bearing for.

WHAT TO LOOK FOR IN THE OUTPUT:
  * FINDINGS are candidates, not verdicts. Every one needs a human decision.
  * The NOT CHECKABLE list at the end is the part that still costs you attention.
    A clean run is not a pass; a clean run plus that checklist is.
  * Exit status is 1 if anything matched, so this can gate a publish script.

  python3 lab/tools/sanitise_gate.py                    # scan the publish paths
  python3 lab/tools/sanitise_gate.py path/to/draft.md   # scan named files
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Paths scanned when invoked with no arguments: everything that can leave the
# building. Relative to the 09-writing root (this file's grandparent).
DEFAULT_SCAN = ("artifacts/04-postmortem", "artifacts/07-posts")

# (label, pattern, why it matters). Deliberately noisy: a false positive costs a
# glance, a false negative costs the thing the gate exists to prevent.
BUILTIN_RULES: list[tuple[str, str, str]] = [
    ("email address", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
     "identifies a person or a company domain"),
    ("internal hostname", r"\b[a-z0-9][a-z0-9.-]*\.(internal|intranet|corp|local|lan|svc|cluster\.local)\b",
     "maps to an internal system"),
    ("private IPv4", r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b",
     "internal topology"),
    ("ticket key", r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b",
     "internal tracker ID; use a description instead"),
    ("absolute currency", r"[$£€]\s?\d[\d,.]*\s?(?:[kKmMbB]|million|billion)?\b",
     "lab/README.md: ratios and percentages, not absolutes"),
    ("k8s/service DNS", r"\bhttps?://[a-z0-9-]+(?:\.[a-z0-9-]+)*:\d{2,5}\b",
     "internal endpoint"),
    ("on-call attribution", r"(?i)\bwas on[- ]call\b|\bon[- ]call engineer\b",
     "the 'who was on call' line is an employee name in disguise"),
]

# The checklist items no regex reaches. Printed every run, on purpose.
NOT_CHECKABLE = [
    "No customer data, and nothing identifying a specific customer's traffic pattern.",
    "No employee names -- including initials, team nicknames, and 'the person who'.",
    "Service names that are internal-only, even when they look generic.",
    "Relative latencies where the absolutes are sensitive.",
    "Employer sign-off IN WRITING before publishing anything derived from a",
    "  production incident. Find the line before publication, not after.",
]


def load_extra_rules(root: Path) -> list[tuple[str, str, str]]:
    """User-maintained patterns: one regex per line, '#' comments ignored.

    This is where your company's own hostname, product and customer patterns go.
    The file is deliberately not committed with real values -- see the .example.
    """
    path = root / "lab" / "tools" / "sensitive-patterns.txt"
    if not path.exists():
        return []
    rules = []
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            re.compile(line)
        except re.error as exc:
            print(f"  ! sensitive-patterns.txt:{lineno} is not a valid regex: {exc}")
            continue
        rules.append((f"local pattern (line {lineno})", line, "from sensitive-patterns.txt"))
    return rules


def collect_targets(root: Path, argv: list[str]) -> list[Path]:
    if argv:
        return [Path(a) for a in argv]
    targets: list[Path] = []
    for rel in DEFAULT_SCAN:
        d = root / rel
        if d.is_dir():
            targets.extend(sorted(p for p in d.rglob("*.md")))
    return targets


def scan(path: Path, rules) -> list[tuple[int, str, str, str]]:
    findings = []
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        print(f"  ! cannot read {path}: {exc}")
        return findings
    for lineno, line in enumerate(text.splitlines(), 1):
        for label, pattern, why in rules:
            m = re.search(pattern, line)
            if m:
                findings.append((lineno, label, m.group(0), why))
    return findings


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    rules = BUILTIN_RULES + load_extra_rules(root)
    targets = collect_targets(root, sys.argv[1:])

    print("SANITISATION GATE -- lab/README.md checklist, mechanical half")
    print(f"root      : {root}")
    print(f"rules     : {len(rules)} ({len(BUILTIN_RULES)} built in)")
    if not targets:
        print("targets   : none")
        print()
        print("No .md files found in the publish paths, so nothing was scanned:")
        for rel in DEFAULT_SCAN:
            state = "exists" if (root / rel).is_dir() else "missing"
            print(f"  {rel}  ({state})")
        print("Pass a path explicitly to scan a draft that lives somewhere else.")
        print_manual()
        return 0
    print(f"targets   : {len(targets)} file(s)")
    print()

    total = 0
    for path in targets:
        findings = scan(path, rules)
        rel = path.relative_to(root) if root in path.parents else path
        if not findings:
            print(f"  clean   {rel}")
            continue
        print(f"  FINDINGS {rel}")
        for lineno, label, match, why in findings:
            print(f"    line {lineno:>4}  {label}: {match!r}")
            print(f"              why: {why}")
        total += len(findings)
    print()
    print(f"GATE: {total} candidate finding(s). Each needs a human decision;")
    print("      this is a grep, not a guarantee.")
    print_manual()
    return 1 if total else 0


def print_manual() -> None:
    print()
    print("NOT CHECKABLE BY THIS SCRIPT -- do these yourself, every time:")
    for item in NOT_CHECKABLE:
        print(f"  [ ] {item}" if not item.startswith("  ") else f"      {item.strip()}")


if __name__ == "__main__":
    raise SystemExit(main())
