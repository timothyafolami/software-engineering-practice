#!/usr/bin/env python3
"""Topic 9: the verb/noun census and the blind-name quiz.

WHAT THIS DEMONSTRATES: naming defects are countable. "Five verbs mean 'one row
by id'" is a census, not a review -- no judgement required to produce it, and
none available to argue with it. The judgement is in what you do next, which is
the exercise.

WHAT TO LOOK FOR: four reports and one quiz.
  verbs    every verb prefix, grouped by the SHAPE of what the function returns,
           so five verbs meaning the same thing land next to each other
  nouns    domain nouns appearing in more than one spelling
  generic  the placeholder census: manager, handler, helper, util, process, ...
  scope    name length vs scope size -- Go's "length scales with scope", measured
  quiz     N public functions, names and signatures ONLY, no bodies, no
           docstrings. Answer three questions about each, then check the key.

This walks the AST rather than grepping, because a regex over `def ` misses
methods, decorated functions and anything defined in a class body -- which is
topic 9's first broken-experiment note.

    python name_audit.py --path 08-craft/lab/api/app
    python name_audit.py --path ~/svc --report verbs,nouns,generic,scope
    python name_audit.py --path ~/svc --quiz 20 > /tmp/quiz.txt
    python name_audit.py --path ~/svc --quiz-key > /tmp/key.txt

Standard library only.
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import random
import re
import sys
from collections import Counter, defaultdict

GENERIC = {
    "manager", "handler", "helper", "util", "utils", "process", "handle",
    "data", "info", "obj", "tmp", "temp", "item", "value", "result", "thing",
    "service", "controller", "wrapper", "base", "common", "misc", "stuff",
    "do", "run", "execute", "perform",
}

# Verbs that in most codebases all mean "one row by id". The point of listing
# them is that a codebase using five of them charges every reader a translation
# table -- the cheapest measurable naming defect there is, and the one nobody counts.
FETCH_VERBS = {"get", "fetch", "load", "read", "find", "retrieve", "lookup", "select", "query"}


def split_name(name: str) -> list[str]:
    """snake_case and camelCase both, so this works on ported code too."""
    name = name.lstrip("_")
    parts: list[str] = []
    for chunk in name.split("_"):
        parts.extend(p for p in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+|[A-Z]", chunk) if p)
    return [p.lower() for p in parts]


class Fn:
    __slots__ = ("name", "file", "line", "end_line", "args", "returns", "is_public",
                 "is_method", "doc", "raises", "mutates")

    def __init__(self, node, file: pathlib.Path, is_method: bool, src_lines: list[str]):
        self.name = node.name
        self.file = file
        self.line = node.lineno
        self.end_line = getattr(node, "end_lineno", node.lineno)
        self.args = [a.arg for a in node.args.args + node.args.kwonlyargs if a.arg not in ("self", "cls")]
        self.returns = ast.unparse(node.returns) if node.returns else None
        self.is_public = not node.name.startswith("_")
        self.is_method = is_method
        self.doc = ast.get_docstring(node)
        self.raises = sorted({
            ast.unparse(n.exc.func) if isinstance(n.exc, ast.Call) else ast.unparse(n.exc)
            for n in ast.walk(node) if isinstance(n, ast.Raise) and n.exc is not None
        })
        # A crude but honest proxy for "mutates an argument": an assignment to
        # a subscript or attribute of a parameter, or a call to a known mutator
        # on one. It is a candidate detector; the quiz key says so.
        names = set(self.args)
        mutates = set()
        for n in ast.walk(node):
            if isinstance(n, (ast.Assign, ast.AugAssign)):
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                for t in targets:
                    root = t
                    while isinstance(root, (ast.Subscript, ast.Attribute)):
                        root = root.value
                    if isinstance(root, ast.Name) and root.id in names and root is not t:
                        mutates.add(root.id)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                if (isinstance(n.func.value, ast.Name) and n.func.value.id in names
                        and n.func.attr in {"append", "extend", "add", "update", "pop",
                                            "clear", "sort", "insert", "remove"}):
                    mutates.add(n.func.value.id)
        self.mutates = sorted(mutates)

    @property
    def scope_lines(self) -> int:
        return max(1, self.end_line - self.line + 1)

    @property
    def verb(self) -> str:
        parts = split_name(self.name)
        return parts[0] if parts else ""

    def signature(self) -> str:
        args = ", ".join(self.args)
        ret = f" -> {self.returns}" if self.returns else ""
        return f"{self.name}({args}){ret}"


def collect(root: pathlib.Path) -> list[Fn]:
    fns: list[Fn] = []
    files = [root] if root.is_file() else sorted(root.rglob("*.py"))
    for f in files:
        if any(p in {".venv", "venv", "node_modules", "__pycache__", ".git", "build", "dist"}
               for p in f.parts):
            continue
        try:
            src = f.read_text()
            tree = ast.parse(src)
        except (SyntaxError, UnicodeDecodeError) as exc:
            print(f"# skipped {f}: {exc}", file=sys.stderr)
            continue
        lines = src.splitlines()
        # Module-level functions and methods only. A closure defined inside
        # another function is not interface surface -- a short local name in a
        # small scope is CORRECT, and counting one as a public name would put
        # noise in every report and, worse, in the quiz.
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        fns.append(Fn(sub, f, True, lines))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fns.append(Fn(node, f, False, lines))
    return fns


def return_shape(fn: Fn) -> str:
    """Group verbs by what comes back, so synonyms line up as adjacent rows."""
    r = (fn.returns or "").replace(" ", "")
    if not r:
        return "unannotated"
    if r in {"None"}:
        return "None"
    if r.startswith(("list", "List", "Sequence", "Iterable", "tuple")):
        return "collection"
    if "|None" in r or r.startswith("Optional"):
        return "one-or-None"
    if r in {"int", "float", "bool", "str", "bytes"}:
        return "scalar"
    return "one"


def report_verbs(fns: list[Fn]) -> None:
    print("== VERBS, grouped by return shape "
          "(synonyms in one group are the translation table your readers pay for)")
    by_shape: dict[str, Counter] = defaultdict(Counter)
    for fn in fns:
        by_shape[return_shape(fn)][fn.verb] += 1
    for shape in sorted(by_shape, key=lambda s: -sum(by_shape[s].values())):
        counts = by_shape[shape]
        print(f"\n  returns {shape}:")
        for verb, n in counts.most_common():
            flag = "  <-- fetch-family" if verb in FETCH_VERBS else ""
            print(f"    {n:4d}  {verb}{flag}")
    fetch_here = {f.verb for f in fns if f.verb in FETCH_VERBS
                  and return_shape(f) in {"one", "one-or-None"}}
    print(f"\n  DISTINCT VERBS MEANING 'one row by id': {len(fetch_here)}  {sorted(fetch_here)}")


def report_nouns(fns: list[Fn]) -> None:
    print("\n== NOUNS in more than one spelling")
    # Known synonym families. A tool cannot discover that `txn` means `order`;
    # it can only check the families you declare, and declaring them is the
    # useful half of the work.
    families = [
        {"order", "purchase", "txn", "transaction", "sale"},
        {"customer", "user", "account", "client", "buyer"},
        {"item", "line", "lineitem", "product", "sku"},
        {"total", "amount", "sum", "value", "price"},
        {"created", "createdat", "timestamp", "ts", "time", "date"},
    ]
    seen: Counter[str] = Counter()
    for fn in fns:
        for w in split_name(fn.name) + [w for a in fn.args for w in split_name(a)]:
            seen[w] += 1
    any_found = False
    for fam in families:
        present = {w: seen[w] for w in fam if seen[w]}
        if len(present) > 1:
            any_found = True
            print(f"  {len(present)} spellings: " +
                  ", ".join(f"{w}({n})" for w, n in sorted(present.items(), key=lambda x: -x[1])))
    if not any_found:
        print("  none of the declared families appear in more than one spelling")


def report_generic(fns: list[Fn]) -> None:
    print("\n== GENERIC NAMES (candidates -- record the defensible ones as defended)")
    hits = [f for f in fns if f.is_public and set(split_name(f.name)) & GENERIC]
    for fn in sorted(hits, key=lambda f: str(f.file)):
        words = sorted(set(split_name(fn.name)) & GENERIC)
        print(f"  {fn.file}:{fn.line}  {fn.name}   ({', '.join(words)})")
    print(f"  public functions with a generic word: {len(hits)} of "
          f"{sum(1 for f in fns if f.is_public)}")


def report_scope(fns: list[Fn]) -> None:
    print("\n== NAME LENGTH vs SCOPE SIZE (Go's rule, measured in Python)")
    ranked = sorted(fns, key=lambda f: (len(f.name) / f.scope_lines), reverse=True)
    print("  longest name in the smallest scope:")
    for fn in ranked[:5]:
        print(f"    {len(fn.name):3d} chars / {fn.scope_lines:3d} lines   "
              f"{fn.name}   {fn.file}:{fn.line}")
    print("  shortest name in the largest scope:")
    for fn in sorted(fns, key=lambda f: (len(f.name) / f.scope_lines))[:5]:
        print(f"    {len(fn.name):3d} chars / {fn.scope_lines:3d} lines   "
              f"{fn.name}   {fn.file}:{fn.line}")


def quiz(fns: list[Fn], n: int, seed: int, key: bool) -> None:
    """Names and signatures only. No bodies. No docstrings.

    Drawn from the PUBLIC surface: low scores on private helpers inside one
    module mean little, because short local names in a small scope are correct.
    """
    pool = [f for f in fns if f.is_public and f.args]
    if not pool:
        sys.exit("no public functions with arguments found; check --path")
    # Draw ONE permutation and take a prefix, rather than sampling `n` directly.
    # `sample(pool, 5)` and `sample(pool, 20)` from the same seed are different
    # sets, so a key printed at a different N would not be the key to the quiz
    # you answered -- which is a silent wrong answer, the worst kind.
    picked = random.Random(seed).sample(pool, len(pool))[: min(n, len(pool))]

    if not key:
        print(f"# BLIND-NAME QUIZ -- {len(picked)} functions, seed {seed}")
        print("# For each: (1) what does it return when the thing does not exist?")
        print("#           (2) does it mutate any argument?")
        print("#           (3) can it raise, and if so with what?")
        print("# Answer from the NAME AND SIGNATURE ALONE. Re-run with --quiz-key "
              f"--quiz-seed {seed} to score.\n")
        for i, fn in enumerate(picked, 1):
            print(f"{i:3d}. {fn.signature()}")
            print(f"     (1) ______  (2) ______  (3) ______")
        return

    print(f"# ANSWER KEY -- seed {seed}")
    print("# 'mutates' is a static candidate detector, not a proof: it flags "
          "assignment into a\n# parameter's subscript/attribute and calls to "
          "known mutators. Check the ones it flags.\n")
    for i, fn in enumerate(picked, 1):
        print(f"{i:3d}. {fn.signature()}")
        print(f"     file      : {fn.file}:{fn.line}")
        print(f"     raises    : {', '.join(fn.raises) or 'nothing explicitly'}")
        print(f"     mutates   : {', '.join(fn.mutates) or 'no argument (by this detector)'}")
        first_doc = (fn.doc or "").strip().splitlines()
        print(f"     docstring : {first_doc[0] if first_doc else '(none)'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", required=True)
    ap.add_argument("--report", default="verbs,nouns,generic,scope")
    ap.add_argument("--quiz", type=int, metavar="N", help="print a blind-name quiz of N functions")
    ap.add_argument("--quiz-key", action="store_true")
    ap.add_argument("--quiz-seed", type=int, default=20260818,
                    help="same seed draws the same functions in the same order, so "
                         "the key matches the quiz")
    args = ap.parse_args()

    root = pathlib.Path(args.path).expanduser()
    if not root.exists():
        sys.exit(f"no such path: {root}")
    fns = collect(root)
    if not fns:
        sys.exit(f"no Python functions found under {root}")

    if args.quiz or args.quiz_key:
        quiz(fns, args.quiz or 20, args.quiz_seed, key=args.quiz_key)
        return 0

    print(f"path      : {root}")
    print(f"functions : {len(fns)} ({sum(1 for f in fns if f.is_public)} public, "
          f"{sum(1 for f in fns if f.is_method)} methods)\n")
    wanted = {r.strip() for r in args.report.split(",")}
    if "verbs" in wanted:
        report_verbs(fns)
    if "nouns" in wanted:
        report_nouns(fns)
    if "generic" in wanted:
        report_generic(fns)
    if "scope" in wanted:
        report_scope(fns)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
