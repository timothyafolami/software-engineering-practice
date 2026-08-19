# Postmortem: <user-visible symptom, not the cause>
Status: Draft | Reviewed · Author · Date · Reviewers:

## Summary
Five sentences max. What users experienced, over what window, how big, and what
changed to end it. Written for someone who will read only this.

## Impact
In user units first: how many requests over what threshold, how many users, how
many failed actions. Then money or SLO burn if you have it. Every number gets its
source: dashboard query, log range, or "unknown — see Detection Gaps".

## Timeline (UTC, one row per observable event)
| Time | What happened | How we knew (or didn't) |
|---|---|---|
| | | |

Start the timeline at the change that made it possible, not at the alert.

## Contributing factors
Numbered, plural. Include the ones that are about the system's shape: a default
that was never chosen, a dependency nobody knew was synchronous, a limit nobody
had read. No factor may name a person.

1.
2.
3.

## Detection gaps — why this was hard to see
Time to detect / time to diagnose / time to mitigate, with the actual clock
times. Then: what were we looking at that told us the wrong thing? What did we
suspect first, and why was that reasonable? What signal would have collapsed the
diagnosis time?

| Clock | Value | How you know |
|---|---|---|
| Time to detect | | |
| Time to diagnose | | |
| Time to mitigate | | |

## What went right
A real section, not a courtesy. Anything that limited the blast radius is a
control you must not accidentally remove later.

## Actions
| # | Action | Changes what | Owner | Due |
|---|---|---|---|---|
| | | | | |

Every action must change the system: a default, a limit, a signal, an interface,
a rollout mechanism. "Be more careful", "add it to the review checklist" and
"document this" are person-changes wearing a system costume. At most one action
may be a doc, and only if the doc is a runbook that shortens diagnosis.

## What we still don't know
