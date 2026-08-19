# Part B (Python) — uv.lock hash verification

```
uv lock                                   # writes uv.lock with per-package hashes
uv export --format pylock.toml -o pylock.toml   # PEP 751 lockfile

# Tamper with one hash in uv.lock (flip a hex digit), then:
uv sync
echo "exit=$?"
```

Record the exact exit code and message: a good lockfile tool REFUSES (non-zero
exit) rather than warning, because a hash mismatch means the bytes on disk are
not the bytes you locked. `uv` with a committed lockfile plus hash verification
is the current posture (PEP 751 / `pylock.toml`). Requires network for the
first `uv lock`; the tamper-then-`uv sync` step is the runnable control.
