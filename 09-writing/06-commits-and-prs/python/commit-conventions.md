# Python — where the reasoning lives, and where it goes when the tracker dies

**The convention.** CPython itself uses `gh-NNNNN: summary` — the subject is a
join key into the issue tracker, and the argument lives in the linked issue. The
wider ecosystem mandates nothing at all: pip, most libraries and most application
repos have no commit format, so whatever your team does is habit rather than
rule.

**The consequence for you.** In a Python codebase the commit body is frequently
*empty by convention*. Nothing is enforcing it, nothing is generating a changelog
from it, and the reasoning sits in a tracker your company may not still be paying
for in three years. Of the six ecosystems here, this is the one where the gap
between "the reasoning exists somewhere" and "the reasoning is reachable from the
repository" is widest, because nothing pushes back when you leave the body blank.

**The failure mode, concretely.** A line reads `# noqa: E501` or
`timeout=30  # see issue`. The issue number is gone, the tracker was migrated,
and the only surviving artifact is a line of code whose justification is a dead
link. The next engineer has two options: keep it out of superstition, or remove
it and find out.

## The shape to write

Bad — passes review, fails archaeology:

```
fix: increase retry count

Increases the retry count from 2 to 5 in the pricing client to make the
integration more reliable.
```

The body restates the diff. A reader in eighteen months learns nothing the diff
does not already say, and cannot tell whether 5 was reasoned or guessed.

Good — carries what the diff destroys:

```
pricing client: retry 5x with jitter, not 2x

Why now: <incident/ticket ref> - checkout failures during the pricing
service's deploy window. Their rolling restart drops connections for
<duration measured from: source>, and 2 retries at <backoff> did not span it.

Rejected first: raising the client timeout instead. That holds the event-loop
thread for the whole window and turns a fast failure into a slow one for every
concurrent request (see <postmortem link>).

Constraint: the pricing call must stay idempotent-safe to retry - it is a pure
quote lookup today, and this commit is only correct while that is true.

Verified: <what you ran, and what you observed>.

Refs: <issue>
```

The **Constraint** paragraph is the one no Python tool can express. There is no
type in the language that says "this call is safe to retry", so the commit
message is the only place that invariant is written down — and the day someone
makes the call non-idempotent, this message is the repository's only warning.

## Archaeology in a Python repo

```bash
git log -L 120,124:app/pricing.py          # every commit that touched those lines
git log --format='%H %s%n%b' -1 <sha>      # the full message, body included

# Python-specific hunting ground: unexplained suppressions and magic constants
grep -rnE "# noqa|# type: ignore|# pragma: no cover" app/ | head -20
grep -rnE "sleep\(|timeout=|retries?=|max_|_LIMIT" app/ | head -20
```

Every `# noqa` is a decision someone made and did not explain. Pick three, time
yourself, and see whether the history answers the question. That is the baseline
measurement the topic asks for, and this is the cheapest place in any Python repo
to find candidate lines.
