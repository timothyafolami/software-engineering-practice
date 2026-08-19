#!/usr/bin/env python3
"""Topic 2: coupling, measured from git history rather than from directories.

WHAT THIS DEMONSTRATES: coupling is not a property of code you can see in one
file -- it is P(B changed | A changed), and your commit history has been
recording that for free the whole time. It very often disagrees with your
package structure, and it catches coupling that has NO IMPORT AT ALL: two
services deployed together, a JSON contract duplicated across a queue, a
migration and the model it matches, a feature flag and the three files reading
it. None of those produce an edge in an import graph. All of them produce one
here.

WHAT TO LOOK FOR: the top pairs, and then your classification of each as
  (a) legitimately one concept in the wrong directory
  (b) a leaky abstraction -- B changes because A's interface does not cover the case
  (c) a file that changes with everything (lockfile, changelog, version header)
The number alone tells you nothing about which fix applies. The classification
is the exercise.

    python temporal_coupling.py --repo ~/path/to/your/service --months 12
    python temporal_coupling.py --repo ~/path/to/your/service --months 12 \
      --exclude 'poetry.lock,pyproject.toml,CHANGELOG.md'

Standard library only. Nothing to install.
"""
from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys
from collections import Counter, defaultdict
from itertools import combinations

DEFAULT_EXCLUDES = [
    "*.lock", "*.sum", "package-lock.json", "yarn.lock", "poetry.lock",
    "CHANGELOG*", "*.snap", "*.min.js", "*.svg", "*.po", "*.mo",
]


def git_log(repo: str, months: int) -> list[tuple[str, list[str]]]:
    """One (sha, files) pair per non-merge commit.

    `--no-merges` matters more than it looks: a merge commit lists every file
    from both sides, which would make every file in a release look co-changed
    with every other and flatten the whole matrix.
    """
    out = subprocess.run(
        ["git", "-C", repo, "log", f"--since={months}.months.ago", "--no-merges",
         "--name-only", "--pretty=format:%x00%H"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        sys.exit(f"git log failed in {repo}:\n{out.stderr.strip()}")

    commits: list[tuple[str, list[str]]] = []
    for chunk in out.stdout.split("\x00"):
        if not chunk.strip():
            continue
        lines = [ln for ln in chunk.splitlines() if ln.strip()]
        commits.append((lines[0], lines[1:]))
    return commits


def excluded(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(path.split("/")[-1], p)
               for p in patterns)


def analyse(commits, *, patterns, max_files, min_changes):
    changes: Counter[str] = Counter()
    pairs: Counter[tuple[str, str]] = Counter()
    dropped_big = 0

    for _sha, files in commits:
        files = sorted({f for f in files if not excluded(f, patterns)})
        if not files:
            continue
        if len(files) > max_files:
            # A formatting sweep or a codegen commit touches everything and
            # normalises the whole matrix toward 1.0. Topic 2's third
            # broken-experiment note is exactly this.
            dropped_big += 1
            continue
        changes.update(files)
        for a, b in combinations(files, 2):
            pairs[(a, b)] += 1

    scored = []
    for (a, b), co in pairs.items():
        if changes[a] < min_changes or changes[b] < min_changes:
            # The floor. Without it, a pair that changed twice together and
            # never apart scores 1.0 and tops the list on no evidence at all.
            continue
        ratio = co / min(changes[a], changes[b])
        scored.append((ratio, co, a, b, changes[a], changes[b]))
    scored.sort(reverse=True)
    return scored, changes, dropped_big


def cohesion(commits, *, patterns, depth: int) -> list[tuple[float, str, int]]:
    """Fraction of a module's commits that touched ONLY that module.

    A module whose commits always drag in three other files is not cohesive, no
    matter how tidily its methods are grouped.
    """
    total: Counter[str] = Counter()
    alone: Counter[str] = Counter()
    for _sha, files in commits:
        files = [f for f in files if not excluded(f, patterns)]
        mods = {"/".join(f.split("/")[:depth]) for f in files}
        for m in mods:
            total[m] += 1
            if len(mods) == 1:
                alone[m] += 1
    return sorted(
        ((alone[m] / total[m], m, total[m]) for m in total if total[m] >= 5),
        reverse=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--min-changes", type=int, default=8,
                    help="floor on each file's own change count (default 8)")
    ap.add_argument("--max-files", type=int, default=50,
                    help="skip commits touching more than this many files")
    ap.add_argument("--exclude", default="",
                    help="comma-separated globs, added to the built-in list")
    ap.add_argument("--module-depth", type=int, default=2,
                    help="path components that define a 'module' for cohesion")
    args = ap.parse_args()

    patterns = DEFAULT_EXCLUDES + [p.strip() for p in args.exclude.split(",") if p.strip()]
    commits = git_log(args.repo, args.months)
    if not commits:
        sys.exit(f"no non-merge commits in the last {args.months} months in {args.repo}")

    scored, changes, dropped = analyse(
        commits, patterns=patterns, max_files=args.max_files, min_changes=args.min_changes
    )

    print(f"repo            : {args.repo}")
    print(f"window          : last {args.months} months")
    print(f"commits analysed: {len(commits) - dropped} ({dropped} skipped for touching "
          f">{args.max_files} files)")
    print(f"files seen      : {len(changes)}")
    print(f"floor           : each file changed >= {args.min_changes} times")
    print()

    if not scored:
        print("No pair cleared the floor. Either the window is too short, or "
              f"--min-changes ({args.min_changes}) is too high for this repo's pace.")
        print("Lower it and re-run -- but say in the record that you did.")
        return 0

    print(f"TOP {min(args.top, len(scored))} PAIRS BY co_changes / min(changes_A, changes_B)")
    print(f"{'ratio':>6} {'co':>4} {'A#':>4} {'B#':>4}  class  files")
    print("-" * 100)
    for ratio, co, a, b, ca, cb in scored[: args.top]:
        # 'class' is left blank on purpose. Classifying each pair as (a), (b) or
        # (c) is the exercise; a tool that guessed would be inventing the finding.
        print(f"{ratio:6.2f} {co:4d} {ca:4d} {cb:4d}  ___    {a}\n{'':>24}       {b}")

    print()
    print("MODULE COHESION -- fraction of a module's commits that touched only it")
    print(f"{'cohesion':>9} {'commits':>8}  module")
    print("-" * 60)
    for frac, mod, tot in cohesion(commits, patterns=patterns, depth=args.module_depth)[:15]:
        print(f"{frac:9.2f} {tot:8d}  {mod}")

    print()
    print("Classify every pair above as (a) one concept in the wrong directory, "
          "(b) a leaky\nabstraction, or (c) a file that changes with everything "
          "-- then add the (c)s to\n--exclude and re-run. The second run is the real one.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
