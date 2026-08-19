# Java — the reasoning is written down, somewhere you may not reach

**The convention.** OpenJDK ties changes to its bug tracker: the subject is the
issue id and synopsis, and larger changes hang off a JEP, which is itself a
durable document with a state and a history. The message is a **join key into a
formal process**, and that process is unusually good — a JEP records motivation,
alternatives and risks the way this layer's Topic 1 asks you to.

**The consequence for you.** This is the ecosystem most likely to have the
reasoning written down *somewhere*, and most likely to have it somewhere you
cannot reach in two years: an internal Jira, a Confluence page, an architecture
board's minutes. Enterprise Java shops have more process than any other
ecosystem here and more of it stored outside the repository.

The habit worth building is therefore the opposite of the ecosystem's instinct:
**write the conclusion into the commit even though the argument lives in the
ticket.** One sentence, in the repository, that survives the tracker migration.

**The failure mode, concretely.** `JIRA-4412: fix pricing retries`. The Jira
instance was consolidated during an acquisition, the project key was remapped,
and the ticket now resolves to something unrelated or to nothing. The change is
still running in production and the reason is unrecoverable.

**One Java-specific case worth its own habit.** Concurrency invariants. Which
lock guards which field, whether a class is safe to publish to another thread,
whether a method may be called from a virtual thread that must not pin its
carrier — none of these are in the type system, and `synchronized` tells you a
lock exists rather than what it protects.

## The shape to write

```
<ISSUE-ID>: pricing client retries 5x with jitter, not 2x

Why now: <incident/ticket ref> - checkout failures during the pricing service's
deploy window. Their rolling restart drops connections for <duration, measured
from: source>, and 2 retries at <backoff> did not span it.

Rejected first: raising the HTTP client timeout. On the platform-thread pool
that holds a worker for the whole window; on virtual threads it would not, but
this service does not run on 21 yet and the change would have hidden the
problem rather than fixed it.

Constraint: PricingClient is shared across request threads and is safe to
publish only because every field is final and the underlying HTTP client is
thread-safe. Adding mutable state here is a data race, not a refactor.

Verified: <what you ran, what you observed, on which JDK>.

Refs: <issue>
```

The **Constraint** paragraph states a publication-safety argument that the
language expresses only indirectly, through `final` fields that a future edit can
quietly remove.

## Archaeology in a Java repo

```bash
git log -L 140,150:src/main/java/com/example/PricingClient.java
git log --format='%H %s%n%b' -1 <sha>

# Java-specific hunting ground: concurrency and configuration decisions
grep -rnE "synchronized|volatile|AtomicI|ThreadLocal|newFixedThreadPool\([0-9]+\)" src/main/java | head -20
grep -rnE "@SuppressWarnings|@Deprecated" src/main/java | head

# and the numbers that came from somewhere
grep -rnE "Duration\.of|TimeUnit\.|\.setMaxTotal\(|maxPoolSize" src/main/java | head

# how many subjects are pure tracker keys with nothing under them?
git log -50 --format='%s%n%b' | grep -cE "^[A-Z][A-Z0-9]+-[0-9]+:?\s*$"
```

`Executors.newFixedThreadPool(8)` is a decision about a machine, made on a day,
under a load. Three of those, timed, is the baseline — and the last command tells
you how much of your history is a join key into a system you may not control.
