# Topic 3 rubric — score the loop, not the document

The document was scored in [Topic 1](../01-the-design-doc/rubric.md). This page
scores the *process*: whether disagreement was asked for, extracted, and kept.

| # | Check | How to test it | Score |
|---|---|---|---|
| 1 | Individual reviewers named, with a decision date, in the header | `sh tools/rfc-check.sh` | |
| 2 | Every ledger row points at a specific place in the document | same script — an empty "where it landed" cell fails | |
| 3 | At least one `rejected` row that still names the condition under which it would have won | same script | |
| 4 | State field current; an accepted doc carries a one-sentence decision a stranger could act on | same script, plus read the decision line out loud | |
| 5 | If this decision replaced an earlier one, the earlier doc says `Superseded by →` | script can only flag the string; the *direction* is yours | |
| 6 | The dissent line is one the dissenter would agree is fair | send it to them before committing it | |

## Item 6 has one honest test and it costs one message

Send the dissenter the sentence and ask: "is this a fair statement of your
position?" Anything else — your memory of the conversation, the thread, your
notes — is your version of their argument, which is the thing a dissent line
exists to prevent.

If they were right, that sentence is the fastest path anyone will have to
understanding what was known at the time. That is the same question a postmortem
asks, one topic from now.

## What an empty ledger means, and what it does not

It does not mean the design is sound. Before you conclude anything, check the
three breaks from [`README.md`](README.md):

- **Tense.** Did you circulate a doc describing code that already exists?
  Reviewers read intent from tense, and a doc describing something already built
  gets rubber-stamped every time.
- **Stake.** Will your reviewer ever operate or extend this? If not, their
  approval is politeness.
- **The calendar.** If the comment window overlapped a reviewer's on-call week,
  you measured their pager, not their opinion.

## Mechanical checks

```sh
# from the 09-writing directory
sh 03-the-rfc-loop/tools/rfc-check.sh                       # your RFC + ledger
sh 03-the-rfc-loop/tools/rfc-check.sh '' 03-the-rfc-loop/worked-example-ledger.md

# is the state actually current?
grep -n "^\*\*Status:" artifacts/03-rfc/rfc-<slug>.md

# the cheapest decision history you will ever have, if the doc lives in a repo
git log --follow -p -- artifacts/03-rfc/rfc-<slug>.md
```

That last command is why the state transitions being commits is a nice accident:
the *when* of each transition is free, and the *why* is Topic 6's problem.
