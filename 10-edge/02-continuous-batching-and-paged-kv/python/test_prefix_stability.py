"""
Layer 10 - Topic 2: the regression test that is the actual deliverable.

What this demonstrates
    Incident replay for prefix-cache collapse, and then the fix, in one
    program: two different requests are rendered both ways, and the test
    asserts the first N characters of the prompt are BYTE-IDENTICAL across
    them. With the volatile string at the tail that holds. With it at the
    head it fails at character 0 -- which is exactly the change a
    well-meaning PR makes when it adds `Current time: ...` to the top of a
    system prompt, and exactly the change no code review catches because
    nothing about it looks like a performance change.

What to look for
    - The `head` layout must FAIL and the `tail` layout must PASS. A test
      that cannot fail is not a test; the failing arm is printed on purpose
      so you can see the guard has teeth.
    - `shared prefix` in characters, and the cacheable 16-token blocks that
      survive. Note that `head` does not lose "some" cache -- it loses all
      of it, because the match is a prefix match.
    - Both layouts send the same number of tokens. Nothing about request
      size changed. The prefill bill tripled anyway.

Runs with no arguments, exits non-zero if the guard is broken:
    python3 python/test_prefix_stability.py

Also collects under pytest, if you would rather wire it into CI:
    pytest python/test_prefix_stability.py
"""

from __future__ import annotations

import sys

from prompt_layout import (BLOCK_SIZE, STABLE_SYSTEM_PROMPT, TOKENIZER_NAME,
                           cacheable_blocks, count_tokens, render,
                           shared_prefix_chars)

# The guard: how much of the rendered prompt must be identical across two
# different requests. One block would be enough to prove the principle;
# this is set to the length of the stable system prompt because that is
# what you are actually paying to cache.
STABLE_PREFIX_CHARS = len(STABLE_SYSTEM_PROMPT)

REQUEST_A = "Current time: 2026-08-18T14:02:11Z | request_id: 3f9a1c2e"
REQUEST_B = "Current time: 2026-08-18T14:02:12Z | request_id: 8b21d704"


def _pair(layout: str):
    return render(REQUEST_A, layout=layout), render(REQUEST_B, layout=layout)


def assert_stable_prefix(layout: str) -> None:
    """The assertion a real PR would add. Fails loudly for `head`."""
    a, b = _pair(layout)
    assert a.text[:STABLE_PREFIX_CHARS] == b.text[:STABLE_PREFIX_CHARS], (
        f"prompt layout {layout!r}: the first {STABLE_PREFIX_CHARS} characters "
        f"differ between two requests, so the prefix cache can key on nothing. "
        f"They first diverge at character {shared_prefix_chars(a.text, b.text)}."
    )


def test_tail_layout_keeps_a_stable_prefix():
    assert_stable_prefix("tail")


def test_head_layout_destroys_the_prefix():
    # The regression, asserted as a regression: this documents that the
    # guard above is capable of failing.
    try:
        assert_stable_prefix("head")
    except AssertionError:
        return
    raise AssertionError("head layout should have destroyed the shared prefix")


def report(layout: str) -> bool:
    a, b = _pair(layout)
    shared_chars = shared_prefix_chars(a.text, b.text)
    total_tokens = count_tokens(a.text)
    shared_tokens = count_tokens(a.text[:shared_chars])
    blocks = cacheable_blocks(shared_tokens)
    try:
        assert_stable_prefix(layout)
        verdict, ok = "PASS", True
    except AssertionError:
        verdict, ok = "FAIL", False

    print(f"  layout PROMPT_VOLATILE={layout}")
    print(f"    prompt tokens (both requests) : {total_tokens}")
    print(f"    byte-identical prefix         : {shared_chars} chars "
          f"/ ~{shared_tokens} tokens")
    print(f"    cacheable {BLOCK_SIZE}-token blocks     : {blocks} "
          f"(partial trailing block never caches)")
    print(f"    tokens re-prefilled per request: ~{total_tokens - blocks * BLOCK_SIZE}")
    print(f"    stable-prefix guard           : {verdict}")
    print()
    return ok


def main() -> int:
    print("Prefix stability guard -- topic 2, run 2 deliverable")
    print(f"  token counter: {TOKENIZER_NAME}")
    print(f"  guard: first {STABLE_PREFIX_CHARS} characters must be identical")
    print(f"  request A volatile: {REQUEST_A}")
    print(f"  request B volatile: {REQUEST_B}")
    print()

    tail_ok = report("tail")
    head_ok = report("head")

    if tail_ok and not head_ok:
        print("RESULT: guard behaves correctly -- tail passes, head fails.")
        print("        Wire assert_stable_prefix('tail') into CI. That single")
        print("        line is what stops this regression returning in six")
        print("        months, when nobody remembers why the timestamp is at")
        print("        the bottom of the prompt.")
        return 0
    if tail_ok and head_ok:
        print("BROKEN: the head layout passed too, so the volatile string is")
        print("        not actually varying between requests. Check REQUEST_A")
        print("        and REQUEST_B differ.")
        return 1
    print("BROKEN: the tail layout failed. The stable system prompt is not")
    print("        stable -- something upstream is templating into it.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
