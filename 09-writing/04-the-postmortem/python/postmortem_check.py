"""
Layer 9 Topic 4 -- the mechanical half of the postmortem rubric.

WHAT THIS DEMONSTRATES: rubric items 2, 4, 6, 7 and 8 run as checks instead of
eyeballed, plus the number that is the primary output of your first pass -- the
count of cells you had to mark `unknown`. Each `unknown` is a detection gap and
each detection gap becomes an action, so this script prints that count first and
loudest.

The checks are deliberately literal:
  * COUNTERFACTUALS ("should have", "if only") describe a world that did not
    exist. Every hit is a sentence to rewrite as "X was not visible because ___",
    which usually turns into an action item by itself.
  * PERSON-SHAPED language in Contributing factors or Actions is the rubric's
    item 7 and 8 failure: an action whose subject is a person changes nothing.
  * NUMBERS WITHOUT A SOURCE in Summary/Impact/Timeline break lab rule 1. Every
    number is either derived on the page or carries where it came from.
  * The THREE CLOCKS have to be present, with values or an honest `unknown`.

WHAT TO LOOK FOR IN THE OUTPUT: the unknown count, then the counterfactual list.
A document with zero unknowns and zero counterfactuals was probably reconstructed
from memory; memory is confident and wrong.

  python3 python/postmortem_check.py                        # artifacts/04-postmortem/*.md
  python3 python/postmortem_check.py path/to/postmortem.md
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

COUNTERFACTUALS = [
    r"should have", r"shouldn't have", r"if only", r"failed to", r"forgot to",
    r"could have (?:caught|prevented|noticed|seen)", r"ought to have",
    r"would have (?:caught|prevented|noticed)", r"neglected to",
]

# Person-shaped subjects. A postmortem factor or action naming one of these is
# describing a human to be improved rather than a system to be changed.
PERSON_SHAPED = [
    r"\bthe (?:engineer|developer|author|reviewer|operator|on-call)\b",
    r"\bsomeone\b", r"\bwhoever\b", r"\bhe\b", r"\bshe\b",
    r"\bteam members?\b", r"\bwas careless\b", r"\bhuman error\b",
]

# Actions that look like system changes and are not.
WEAK_ACTIONS = [
    r"be more careful", r"remember to", r"review checklist", r"add it to the checklist",
    r"train the", r"communicate", r"raise awareness", r"encourage", r"remind",
    r"\bdocument (?:this|the)\b",
]

# A line with a number is fine if it also says where the number came from.
SOURCE_MARKERS = [
    r"source", r"from `", r"from <", r"dashboard", r"query", r"log range", r"logs?\b",
    r"unknown", r"`<", r"deploy log", r"ticket", r"measured", r"pg_stat", r"see detection gaps",
]

SECTION_RE = re.compile(r"^##+\s+(.*)$")
NUMBER_RE = re.compile(r"(?<![\w`<])\d+(?:[.,]\d+)?(?![\w>])")
CLOCKS = ("time to detect", "time to diagnose", "time to mitigate")


def sections(text: str) -> list[tuple[str, int, str]]:
    """(section title, 1-based line number, line) for every line in the doc."""
    out, current = [], "(preamble)"
    for lineno, line in enumerate(text.splitlines(), 1):
        m = SECTION_RE.match(line)
        if m:
            current = m.group(1).strip()
            continue
        out.append((current, lineno, line))
    return out


def hits(patterns: list[str], line: str) -> list[str]:
    found = []
    for p in patterns:
        m = re.search(p, line, re.IGNORECASE)
        if m:
            found.append(m.group(0))
    return found


def in_section(name: str, *keys: str) -> bool:
    low = name.lower()
    return any(k in low for k in keys)


def check(path: Path) -> int:
    text = path.read_text(errors="replace")
    lines = sections(text)
    problems = 0

    unknowns = [(ln, l) for _, ln, l in lines if re.search(r"\bunknown\b", l, re.IGNORECASE)]
    print(f"=== {path}")
    print(f"  UNKNOWNS: {len(unknowns)} line(s) containing 'unknown'.")
    print("            This is the primary output of pass 1. Each one is a detection")
    print("            gap; each detection gap becomes an action.")
    if not unknowns:
        print("            Zero unknowns. Check that you did not reconstruct this from")
        print("            memory -- a timeline whose third column is always full is a story.")
    print()

    print("  COUNTERFACTUALS (rubric 6) -- rewrite each as 'X was not visible because ___':")
    cf = 0
    for sec, ln, line in lines:
        for h in hits(COUNTERFACTUALS, line):
            print(f"    line {ln:>4} [{sec}] {h!r}")
            print(f"      {line.strip()[:100]}")
            cf += 1
    print(f"    {cf} found." if cf else "    none.")
    problems += cf
    print()

    print("  PERSON-SHAPED LANGUAGE (rubric 7, 8) in factors and actions:")
    ps = 0
    for sec, ln, line in lines:
        if not in_section(sec, "contributing", "action", "summary"):
            continue
        for h in hits(PERSON_SHAPED, line):
            print(f"    line {ln:>4} [{sec}] {h!r}")
            ps += 1
        for h in hits(WEAK_ACTIONS, line):
            print(f"    line {ln:>4} [{sec}] weak action: {h!r} -- what system does this change?")
            ps += 1
    print(f"    {ps} candidate(s)." if ps else "    none.")
    problems += ps
    print()

    print("  NUMBERS WITH NO SOURCE (lab rule 1) in Summary / Impact / Timeline:")
    ns = 0
    for sec, ln, line in lines:
        if not in_section(sec, "summary", "impact", "timeline"):
            continue
        if line.strip().startswith(("|---", "| ---")) or not NUMBER_RE.search(line):
            continue
        if any(re.search(p, line, re.IGNORECASE) for p in SOURCE_MARKERS):
            continue
        print(f"    line {ln:>4} [{sec}] {line.strip()[:96]}")
        ns += 1
    print(f"    {ns} candidate(s) -- each needs a source or an honest 'unknown'." if ns else "    none.")
    problems += ns
    print()

    low = text.lower()
    missing = [c for c in CLOCKS if c not in low]
    if missing:
        print(f"  THREE CLOCKS: missing {', '.join(missing)}.")
        print("    A long detect with a short mitigate is a monitoring problem wearing")
        print("    an incident costume -- you cannot see that without all three.")
        problems += len(missing)
    else:
        print("  THREE CLOCKS: all three named.")
    print()

    print("  NOT CHECKABLE HERE -- rubric 1, 3 and 5 need a reader:")
    print("    [ ] Can someone who was not there state, from the Summary alone,")
    print("        what users experienced?")
    print("    [ ] Does the timeline start at the change that made it possible, not the alert?")
    print("    [ ] Does the detection-gap section defend the first hypothesis as reasonable")
    print("        at the time? If it looks stupid in the writeup, hindsight removed the lesson.")
    print()
    print(f"  {problems} mechanical problem(s) in {path.name}.")
    return problems


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    args = sys.argv[1:]
    if args:
        targets = [Path(a) for a in args]
    else:
        targets = sorted((root / "artifacts" / "04-postmortem").glob("*.md"))
        if not targets:
            print("No drafts in artifacts/04-postmortem/.")
            print("  cp templates/postmortem.md artifacts/04-postmortem/latency-incident.md")
            print("")
            print("Or read the worked example first, and check it:")
            print("  python3 04-the-postmortem/python/postmortem_check.py \\")
            print("          04-the-postmortem/worked-example.md")
            return 0
    total = 0
    for t in targets:
        if not t.is_file():
            print(f"skip (not a file): {t}")
            continue
        total += check(t)
        print()
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
