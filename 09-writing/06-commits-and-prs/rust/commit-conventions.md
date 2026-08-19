# Rust — the commit is the implementation of an argument recorded elsewhere

**The convention.** The language's decisions live in RFCs. A rustc commit is
frequently the *implementation* of an argument that was had somewhere else and
settled somewhere else, linked by number: the message points at RFC `NNNN` or the
tracking issue, and the reasoning is in that document, in public, permanently.

**The consequence for you.** This is the ecosystem whose model is worth stealing
wholesale: **link the change to the durable argument.** That loop is exactly what
[Topic 3](../../03-the-rfc-loop/README.md) builds — an RFC with a state, a
decision line, and a dissent line — and a commit that names it converts your
repository into an index of your decisions. `git log --grep=RFC` becomes a
history of why, not what.

It also inverts the usual failure mode. In Python the reasoning is missing; in
Rust the reasoning is often excellent and *elsewhere*, so the risk is a link that
outlives the thing it points at.

**The failure mode, concretely.** `See internal RFC 14.` The RFC lived in a wiki
that was migrated, the numbering restarted, and the message now points at a
different document that is confidently about something else. A link is only as
durable as the system holding it — which is the argument for putting the
*conclusion* in the commit even when the argument is linked.

**One Rust-specific case worth its own habit.** `unsafe` blocks and `unwrap()`
calls encode an invariant the compiler agreed to stop checking. The commit that
introduces one is the only place the justification can live in prose, and it is
the difference between a reader trusting the block and rewriting it. The
Cloudflare outage in [Topic 4](../../04-the-postmortem/README.md) is the public
version of this: a `.unwrap()` on an error path, panicking a thread, and the
reasoning for why that path was believed unreachable is exactly what a commit
message is for.

## The shape to write

```
pricing: retry 5x with jitter, not 2x

Why now: <incident/ticket ref> - checkout failures during the pricing service's
deploy window. Their rolling restart drops connections for <duration, measured
from: source>, and 2 retries at <backoff> did not span it.

Rejected first: raising the request timeout instead. On the current runtime that
holds a task, not a thread, so it degrades quietly rather than loudly - the fast
failure is the one that gets noticed and fixed.

Constraint: the retry is only sound because the call is a pure quote lookup.
There is no type in this crate that says so; if it ever writes, this loop is a
duplicate-charge bug.

Verified: <what you ran, and what you observed>.

Refs: <RFC or decision doc>, <issue>
```

The **Constraint** paragraph is doing work the type system cannot do here, and
that is worth noticing precisely because Rust's type system does so much else. A
`&mut` proves exclusivity; nothing proves idempotence.

## Archaeology in a Rust repo

```bash
git log -L 88,96:src/pricing.rs
git log --format='%H %s%n%b' -1 <sha>

# Rust-specific hunting ground: the places the compiler was told to stand down
grep -rnE "unsafe|unwrap\(\)|expect\(|#\[allow\(" src/ | head -20

# and the ones where a decision was pinned
grep -nE "^\s*[a-z0-9_-]+ = \"=?[0-9]" Cargo.toml
git log --oneline -- Cargo.lock | head
```

Every `#[allow(...)]` is a lint someone silenced deliberately. Three of those,
timed, is your baseline measurement — and if the history answers "why" for even
one of them, this ecosystem's habit of linking to a durable argument is the
reason.
