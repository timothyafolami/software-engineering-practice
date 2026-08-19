# Go — one logical change per commit, and the reasoning in the body

**The convention.** The Go project uses `package: short summary` as the subject —
lowercase, no trailing period, the package first so `git log --oneline` reads as
a table of contents. The body carries the reasoning, and review happens on
Gerrit changes rather than GitHub branches, so a change is revised in place and
lands as **one commit with one final message**.

**The consequence for you.** Go's culture assumes one logical change per commit,
and that assumption is what makes `git log -L` useful at all. If a commit does
three things, the history for any one of those lines is polluted by the other
two, and archaeology on that line returns a message that is mostly about
something else. The discipline is not aesthetic; it is what determines whether
the retrieval test in this topic can succeed.

**The failure mode, concretely.** A squashed branch called "address review
feedback" containing the real change, three renames, and a dependency bump. Six
months later `git log -L` on the interesting line returns that commit, and the
message describes the renames.

## The shape to write

```
pricing: retry 5x with jitter, not 2x

Why now: <incident/ticket ref> - checkout failures during the pricing service's
deploy window. Their rolling restart drops connections for <duration, measured
from: source>, and 2 retries at <backoff> did not span it.

Rejected first: raising the client timeout. A longer timeout does not free the
caller - and unlike the Python service, a blocked goroutine here costs a
goroutine rather than the thread, so the pressure to fix it would have been
invisible until the connection pool ran out.

Constraint: the call must stay safe to retry. It is a pure quote lookup today
and this change is only correct while that holds.

Verified: <what you ran, and what you observed>.

Fixes #<issue>
```

The `Fixes #NNNN` trailer is the machine-readable slice; the prose above it is
the part no tooling generates. Both, not one.

## Archaeology in a Go repo

```bash
git log -L 60,68:internal/pricing/client.go
git log --format='%H %s%n%b' -1 <sha>

# Go-specific hunting ground: tuned constants and deliberate buffer sizes
grep -rnE "time\.(Second|Millisecond)|make\(chan .*, [0-9]+\)|GOMAXPROCS|Retry" . | head -20

# does this repo actually keep one logical change per commit?
git log -50 --shortstat --format='%h %s' | paste - - - | head -20
```

A buffered channel with capacity 128 is a decision with a reason, and the reason
is never in the code. Neither is the choice of `time.Second` over
`500*time.Millisecond`. Those are your three lines for the timed retrieval test.
