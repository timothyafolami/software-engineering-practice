# Topic 1 rubric — score yourself before you send

Seven items from [`README.md`](README.md), turned into checks you can actually
run. Score honestly; the doc is not the artifact, the disagreement it draws is,
and a doc that fails item 2 cannot draw any.

Copy this block into your draft's PR/review request, or keep it beside the doc.

| # | Check | How to test it | Score |
|---|---|---|---|
| 1 | Every constraint a reader needs to evaluate the proposal is in **Context** | Give the doc to someone and count the clarifying questions. Each one is a missing Context line | |
| 2 | At least one sentence a competent engineer could disagree with **on the merits** | Underline it. Negate it. Would anyone write the negation? | |
| 3 | Every goal implies a measurement | For each goal, name the graph or query. A goal with no graph is a mood | |
| 4 | Every non-goal is something a reader would otherwise assume was in scope | Cross out any non-goal nobody would have assumed. It was filler | |
| 5 | You state explicitly what you will **not** know until you ship | Search for it. If it is absent, you have implied certainty you do not have | |
| 6 | Rollback is described in **units of time** | "We can revert" fails. "One flag flip, under two minutes, no migration" passes | |
| 7 | Context + Alternatives is not dwarfed by Proposed design | `sh tools/section-balance.sh <file>` — fails above 2:1 | |

## The two items that are worth more than the other five

**Item 2** is the whole layer. If you cannot underline a sentence, you have
written something unfalsifiable, and the review that comes back will be about
wording, because wording is the only thing left to comment on.

**Item 5** is the one people skip because it feels like weakness. It is the
opposite: it is the sentence that tells a reviewer where the seam is, and it is
usually where the best objection comes from.

## Before you send, not after

- Named reviewers, at least one of whom you expect to push back. A doc
  circulated only to people who will agree with it is a diary.
- A decision date in the header. A doc with no requested decision has no deadline
  pressure and reliably draws no disagreement regardless of quality.
- The **Predicted** row of the table in [`README.md`](README.md) filled in, and
  the row in [`../log.md`](../log.md) started. Predictions written after the
  replies land measure nothing.

## Mechanical checks, all of them

```sh
# from the 09-writing directory
sh 01-the-design-doc/tools/section-balance.sh              # item 7, every draft
sh 01-the-design-doc/tools/section-balance.sh 01-the-design-doc/worked-example.md

# item 3, crude but honest: goals that name no measurement
awk '/^## Goals/,/^## Non-goals/' artifacts/01-design-doc/<slug>.md \
  | grep -nEiv "measured by|from <|dashboard|query|p9[59]|rate|graph" | grep -E "^[0-9]+:[0-9]"

# the adjectives that survive are usually the sentences doing no work
grep -nEi "scalable|maintainable|clean|robust|simple|flexible|best practice" \
  artifacts/01-design-doc/<slug>.md
```

That last grep is not a style check. Every hit is a candidate for the
negation test in item 2 — run it on the sentence, and if nobody would write the
negation, the sentence is a noise floor and deleting it costs you nothing.
