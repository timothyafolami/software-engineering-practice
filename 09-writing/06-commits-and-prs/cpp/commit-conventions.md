# C++ — the sharpest version of the whole topic

**The convention.** Large C++ codebases have some of the strictest commit hygiene
anywhere. LLVM prefixes the subject with the component in brackets — `[X86]`,
`[clang]` — and Chromium uses `component: summary` with structured `Bug:` and
`Change-Id:` trailers. Both projects expect a body that explains the change, and
both review at a granularity that assumes the message will be read years later
by someone bisecting.

**Why the strictness is here and not elsewhere.** Three things compound: the code
is long-lived, the invariants are frequently unwritable in the type system, and a
subtle change in a header is invisible in review because its effect is at every
call site rather than at the diff. This is the language where the compiler
records the least about intent, so the message carries the most.

**The consequence for you.** In C++ the commit message is not documentation of
the change; it is frequently the *only* place an ownership rule, a lifetime
assumption, an alignment requirement, or a "this must not allocate" constraint
exists at all. `// NOLINT`, a hand-rolled memory-order argument, a `reinterpret_cast`,
a magic buffer size — each is a claim about the world that the language will
happily let you make and will never check.

**The failure mode, concretely.** A `std::memory_order_relaxed` on an atomic,
committed with the message "optimize counter". Two years later someone needs to
know whether the relaxed ordering was reasoned about or copied from a blog post,
and there is no way to find out short of re-deriving the argument — which is
exactly the cost the original author could have removed with three sentences.
The same trap ate Layer 1 of this lab from the other direction: an optimiser
hoisted an increment out of a loop and the experiment reported zero lost updates
(see the lab [root README](../../../README.md)). Both are "the compiler did
something the source does not say", and both are fixed by writing down what you
believed.

## The shape to write

```
[pricing] retry 5x with jitter, not 2x

Why now: <incident/ticket ref> - checkout failures during the pricing service's
deploy window. Their rolling restart drops connections for <duration, measured
from: source>, and 2 retries at <backoff> did not span it.

Rejected first: raising the socket timeout. The worker pool here is fixed size,
so a longer timeout occupies a worker for the whole window and the queue behind
it is invisible in our metrics - a slow failure instead of a fast one.

Constraint: the retry loop reuses the request buffer. It is safe only while the
transport does not retain a pointer to it after send() returns. Nothing in the
signature says so; if that changes, this is a use-after-free, not a bug in the
retry count.

Verified: <what you ran, what you observed, which build flags>. Note the build
flags explicitly: an -O2 result and an -O0 result are different experiments.

Bug: <issue>
```

The Constraint paragraph here is not a nicety. It is a memory-safety argument
that exists nowhere else in the repository.

## Archaeology in a C++ repo

```bash
git log -L 210,218:src/pricing_client.cc
git log --format='%H %s%n%b' -1 <sha>

# C++-specific hunting ground: claims the compiler agreed not to check
grep -rnE "reinterpret_cast|const_cast|memory_order|NOLINT|#pragma|alignas|volatile" src/ | head -20
grep -rnE "\[\[maybe_unused\]\]|static_assert" src/ | head

# headers are where invisible changes live
git log --oneline --stat -- include/ | head -30
```

Pick three: one `memory_order`, one cast, one magic constant. Time yourself. This
is the ecosystem where the retrieval test is most likely to fail and most likely
to hurt when it does — which is precisely why it is the best place to run it.
