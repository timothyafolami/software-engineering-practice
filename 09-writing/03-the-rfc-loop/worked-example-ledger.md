> **Worked example, not your artifact.** Yours goes in
> `artifacts/03-rfc/ledger.md`, from [`ledger-template.md`](ledger-template.md).
> Names are `<placeholders>`; so are numbers, per
> [`../lab/README.md`](../lab/README.md) rule 1.
>
> This ledger belongs to the RFC in
> [`../01-the-design-doc/worked-example.md`](../01-the-design-doc/worked-example.md)
> — move the pricing call off the event loop under a timeout budget.

# Disagreement ledger — `move-pricing-off-loop`

| # | Raised by | Objection | Verdict | Reason | Where it landed in the doc |
|---|---|---|---|---|---|
| 1 | `<pricing owner>` | The budget assumes pricing fails fast, but it has no timeout of its own, so a slow upstream still consumes our whole budget | accepted | Correct, and I had assumed the opposite without checking | New risk: "the budget is only as tight as the deepest call without one". New non-goal ruling out fixing pricing in this change |
| 2 | `<pager carrier>` | We should move the whole checkout path to a queue instead | rejected for now | Solves a different problem — durability, not latency — and costs a component we have no operational experience with. **Flips if we need at-least-once delivery for checkout**, which is on the roadmap and not scoped | Alternatives, as a fourth entry, with that flip condition |
| 3 | `<pager carrier>` | What does the user see when the budget is exceeded? | accepted | Not an engineering decision and it was filed as a risk, which would have made it mine by default | Moved from Risks to Open questions, with `<product owner>` named as decider |
| 4 | `<pricing owner>` | `<budget>` is below the pricing service's own p99, so this sheds their slow tail on purpose | accepted | True, and worth saying out loud rather than discovering on a graph | Open question 2, plus an explicit line in Proposed design that this is a deliberate choice |
| 5 | `<staff engineer>` | Just raise the pool, it ships today | rejected → partly accepted | Version C of the alternatives found the assumption underneath: the two are not mutually exclusive. Adopted as an interim mitigation, decision on the fix unchanged | Alternatives entry 1 changed from "rejected" to "accepted as interim mitigation"; Rollout gained a step 0 |
| 6 | `<reviewer>` | Could we cache prices? | not understood — asked again | The objection as stated was a suggestion, not a disagreement; asked what failure they were picturing and got "none, just thinking aloud" | Nowhere, and that is recorded here so it is not re-raised as if it were new |

## What the rows are doing

**Row 2 is the one people skip.** It lost, and writing it down is what stops the
same suggestion arriving again in three months as if it had never been
considered. It carries a flip condition for the same reason a rejected
alternative does.

**Row 3 changed the shape of the document, not the design.** A risk is something
you have accepted; an open question is something you are asking someone to
decide. Filing this one as a risk would have made the author the decider by
default, silently.

**Row 5 is the version C result** from
[Topic 2](../02-rejected-alternatives/worked-example/version-c.md). A decision
that changed *after* the doc was drafted is exactly what the ledger preserves —
`git log` on the RFC file shows that it changed, and only the ledger says why.

**Row 6 ends in "nowhere", honestly.** The rule is that every row lands
somewhere in the document; the exception is a row that establishes there was
nothing to land, and that only counts if you went back and asked. Silence in this
column is a row you did not finish.

---

## The header block, once decided

```markdown
**Status:** Accepted · `<date>`
**Decision:** Move the outbound pricing call to an async client under an explicit
`<budget>` timeout, behind `<flag>`, after raising `pool_size` as an interim
mitigation.
**Dissent on record:** `<staff engineer>` held that the pool change alone was
sufficient and that the async work should wait for evidence that the pool did not
fix it. We proceeded because the pool change does not bound the event-loop stall,
only the queue behind it — but that position is on the record, and if the pool
change alone holds `<goal 2>` for `<duration>` it was right and this RFC should be
superseded.
```

The last clause is the part worth copying: it names what would make the dissenter
correct, which is the only version of a dissent line that can ever be settled.
