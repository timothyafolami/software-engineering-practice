"""
Layer 10 - Topic 2: prompt layout, and the one byte that costs you the
whole prefix cache.

This is the single source of truth for how the lab gateway renders a
prompt. `../../lab/docker-compose.yml` mounts this directory into the
gateway container rather than copying it, so the running service and the
regression test in test_prefix_stability.py cannot drift apart. That is
the point of the deliverable: the test is worthless if it checks a
different code path from the one production uses.

The mechanism it models
    A paged-KV engine hashes each COMPLETE block of prompt tokens (16 by
    default) and keys cached KV blocks by that hash. A later request whose
    leading blocks hash identically reuses them. Two consequences:

      - it is a PREFIX match, so one differing byte near position 0
        invalidates every block after it;
      - it is BLOCK-ALIGNED, so a shared prefix of 100 tokens caches only
        the first 96 -- six blocks of 16, and the remaining 4 tokens are
        recomputed on every request forever.

    Hence `PROMPT_VOLATILE=head` versus `PROMPT_VOLATILE=tail`: the same
    two strings, the same total length, ~0% versus ~100% hit rate.

Token counting
    Uses a real tokenizer if `transformers` or `tiktoken` happens to be
    installed, and a whitespace/punctuation approximation otherwise. The
    approximation is labelled everywhere it is used, and it is only ever
    used for BLOCK COUNTING. The byte-identity assertion -- the thing the
    regression test actually enforces -- needs no tokenizer at all and is
    exact either way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

BLOCK_SIZE = 16  # vLLM V1 default; check your engine's config before trusting it

# A stable system prompt long enough to be worth caching. Roughly 2000
# tokens once repeated -- the exact count is printed at runtime rather than
# asserted here.
_SYSTEM_PARAGRAPH = (
    "You are a support assistant for an internal platform team. Answer only "
    "from the runbooks provided. If the answer is not in the runbooks, say so "
    "and name the runbook that should have covered it. Never invent a command. "
    "Prefer the smallest reversible action. Quote exact flag names. "
)
STABLE_SYSTEM_PROMPT = (_SYSTEM_PARAGRAPH * 34).strip()


def _tokenizer():
    """Return (name, callable) for the best token counter available."""
    try:
        import tiktoken  # type: ignore

        enc = tiktoken.get_encoding("cl100k_base")
        return "tiktoken/cl100k_base", lambda s: len(enc.encode(s))
    except Exception:
        pass
    try:
        from transformers import AutoTokenizer  # type: ignore

        tok = AutoTokenizer.from_pretrained("gpt2")
        return "transformers/gpt2", lambda s: len(tok.encode(s))
    except Exception:
        pass

    pattern = re.compile(r"\w+|[^\w\s]")
    return ("APPROXIMATION (no tokenizer installed)",
            lambda s: len(pattern.findall(s)))


TOKENIZER_NAME, count_tokens = _tokenizer()


@dataclass(frozen=True)
class RenderedPrompt:
    text: str
    layout: str
    volatile: str

    @property
    def approx_tokens(self) -> int:
        return count_tokens(self.text)


def render(volatile: str, layout: str = "tail",
           user_message: str = "How do I roll back a bad deploy?") -> RenderedPrompt:
    """Render the full prompt with the per-request volatile string at
    `head` (before the stable system prompt) or `tail` (after it).

    Nothing else differs between the two layouts. Same bytes, same length,
    same model, same everything -- only the position moves.
    """
    if layout not in ("head", "tail"):
        raise ValueError(f"layout must be 'head' or 'tail', got {layout!r}")
    if layout == "head":
        body = f"{volatile}\n\n{STABLE_SYSTEM_PROMPT}"
    else:
        body = f"{STABLE_SYSTEM_PROMPT}\n\n{volatile}"
    return RenderedPrompt(text=f"{body}\n\nUser: {user_message}\nAssistant:",
                          layout=layout, volatile=volatile)


def shared_prefix_chars(a: str, b: str) -> int:
    """Length of the byte-identical leading run of two rendered prompts."""
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


def cacheable_blocks(shared_tokens: int, block_size: int = BLOCK_SIZE) -> int:
    """Complete blocks a prefix cache can actually key on.

    Integer division, not rounding: a partial trailing block is never
    cached, which is why a 100-token shared prefix caches 96 tokens.
    """
    return shared_tokens // block_size
