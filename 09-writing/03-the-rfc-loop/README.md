# Layer 9 · Topic 3 — The RFC as a disagreement-extraction tool

### The takeaway (read this first)

**The one idea:** a design doc without a process is a diary. The process is four
things — **named reviewers, a deadline, an explicit state, and a written
decision** — and its entire purpose is to make disagreement arrive while it is
still cheap.

**Why it matters in practice:** disagreement is not optional. It happens whether
you plan for it or not. The only variable you control is *when*: at doc time
(hours), at code review (days, plus the sunk-cost fight), or in production (an
incident and a rollback). Uber's RFC process is the best-documented public case
of an organisation adopting this deliberately as it scaled from tens to thousands
of engineers — see [The Pragmatic Engineer, *Scaling Engineering Teams via
Writing Things Down: RFCs*](https://blog.pragmaticengineer.com/scaling-engineering-teams-via-writing-things-down-rfcs/).

**You'll know it landed when:** the number of "why did you do it this way?"
comments on your PRs drops, because that conversation already happened in the
doc.

## The concept

### What the process adds that the document does not

A design doc answers *what should we build*. An RFC adds *who has to agree, by
when, and what happens to the objection that does not win*. That last part is the
one people skip, and it is the one that determines whether the process
compounds.

An objection that is raised, considered, and rejected must be **written into the
document** along with its reason. If it is not, it comes back — at code review,
in production, and in the postmortem — each time as if it were new, and each time
costing the full price of the argument again. A process that resolves objections
without recording them is not a process, it is a series of conversations.

### The state field does more work than it looks like

Four words, each of which tells a reader what to do:

- **Draft** — do not spend your good ideas yet; this will change under you.
- **In review** — the clock is running, and silence now is a decision.
- **Accepted** — the argument is closed unless you have new information. This is
  the one that saves time, because it converts "why are we doing it this way"
  from an open question into a lookup.
- **Superseded by →** — the cheapest habit in this entire layer. It is what stops
  a docs folder from becoming a swamp of confidently-wrong archaeology, and it
  costs one line at the moment you would otherwise have deleted the old doc or,
  worse, left it looking current.

### The deadline is the mechanism, not the ceremony

Comments arrive at the deadline or not at all. This is not cynicism about
colleagues; it is the same queueing behaviour you have been studying all lab —
review is unprioritised work competing with prioritised work, and only a deadline
gives it a priority. A doc circulated with "let me know what you think" has
requested nothing and will get it.

The two-week loop below is the shortest cadence that does not exclude the person
who was on call. Shorter loops systematically silence exactly the reviewers whose
objections are worth the most.

### The dissent line

The habit that makes people most uncomfortable is recording, permanently, who
disagreed and why you proceeded anyway. It is also the one that makes the
document worth keeping: it is the difference between "we decided X" and "we
decided X knowing Y, and here is Y in the words of the person who believed it."
When the decision goes wrong, the dissent line is the fastest path to
understanding what was known at the time — which is the same question a
postmortem asks, three topics from now.

### Solo, or a team of two

The process still works, minus the ceremony: a state, a date, one named reader,
and a decision line at the top of the doc. What you lose is breadth of review.
What you keep is the entire benefit of having written the decision down *before*
you were emotionally invested in it, which is most of the value and is available
to a team of one.

### Study these — they are public and they are real

- [Rust RFCs](https://github.com/rust-lang/rfcs) — pick any RFC with a long
  comment thread and watch a real design change under pressure. This is the
  closest thing to a recording of the skill being exercised.
- [Oxide RFD 1](https://rfd.shared.oxide.computer/rfd/0001) and the accompanying
  post [*RFD 1: Requests for Discussion*](https://oxide.computer/blog/rfd-1-requests-for-discussion)
  — the clearest published description of the lifecycle mechanics.
- [The Pragmatic Engineer's list of companies using RFCs, with public
  examples](https://blog.pragmaticengineer.com/rfcs-and-design-docs/).

## How each language actually gets there

**This topic uses none.** The mechanism is organisational — who is asked, by
when, and what is recorded — and it is identical whether the change is to a
Python service, a Go service, or a Postgres schema.

Worth noting for calibration, though: the two best public examples of the process
are language projects (Rust's RFCs, Go's proposal process), and that is not a
coincidence. A language cannot roll back a shipped decision, so the cost of being
wrong is unusually visible, and the process grew to match. Your service can roll
back, which is why it is tempting to skip the process — and also why the rollback
section in Topic 1 is load-bearing.

## The experiment

### The artifact

Convert the Topic 1 doc into a circulated RFC and keep a **disagreement ledger**
in `artifacts/03-rfc/`:

| # | Raised by | Objection | Verdict | Reason | Where it landed in the doc |
|---|---|---|---|---|---|
| | | | | | |

Every row must end somewhere in the document — a changed design, a new
alternative, a new risk, or a new non-goal. An objection that produces no diff
either was not understood or was not real; decide which, out loud, in the Reason
column.

Then add to the top of the doc, permanently:

```markdown
**Status:** Accepted · <date>
**Decision:** <one sentence: what we are doing>
**Dissent on record:** <who disagreed with what, and why we proceeded anyway>
```

### Worked example — a ledger row that did its job

An objection that produced a diff:

> **Raised by** `<name>`. **Objection:** the timeout budget assumes the pricing
> service fails fast, but it currently has no timeout of its own, so a slow
> upstream will still consume our whole budget. **Verdict:** accepted.
> **Reason:** correct, and I had assumed the opposite without checking.
> **Where it landed:** new risk ("budget is only as tight as the deepest call
> without one"), plus a non-goal ruling out fixing pricing in this change.

An objection that lost, and was recorded anyway:

> **Raised by** `<name>`. **Objection:** we should move to a queue for the whole
> checkout path instead. **Verdict:** rejected for now. **Reason:** it solves a
> different problem (durability, not latency) and costs a new component we have
> no operational experience with; **it flips if we need at-least-once delivery
> for checkout**, which is on the roadmap but not scoped. **Where it landed:**
> Alternatives, with that flip condition.

The second row is the one people skip. It is also the one that stops the same
suggestion arriving again in three months.

### Rubric

1. Does the doc name individual reviewers, with a decision date, in the header?
2. Does every ledger row point at a specific place in the document?
3. Is there at least one row with verdict `rejected` that still names the
   condition under which the objection would have won?
4. Is the state field current, and does an accepted doc carry a one-sentence
   decision line a stranger could act on?
5. If the decision replaced an earlier one, does the earlier doc say
   `Superseded by →`?
6. Is there a dissent line, and is it written in a way the dissenter would agree
   is a fair statement of their position?

## How to run

The two-week loop, sustainable indefinitely:

- **Monday, week 1** — circulate. Named reviewers, state `In review`, decision
  date in the header.
- **Friday, week 2** — comments close.
- **Monday, week 3** — decision written into the doc; state becomes `Accepted`;
  the dissent line goes in.

Longer than that and the doc goes stale under the reviewers. Shorter and you
exclude whoever was on call.

The ledger form is [`ledger-template.md`](ledger-template.md), and
[`worked-example-ledger.md`](worked-example-ledger.md) is it filled in for the
Topic 1 doc — six rows, including the one that lost, the one that moved a risk
into an open question, and the one that ended in "nowhere" honestly.

```bash
cd 09-writing
cp artifacts/01-design-doc/<slug>.md artifacts/03-rfc/rfc-<slug>.md
cp 03-the-rfc-loop/ledger-template.md artifacts/03-rfc/ledger.md
$EDITOR artifacts/03-rfc/rfc-<slug>.md artifacts/03-rfc/ledger.md
```

Rubric items 1–4 are mechanical. [`tools/rfc-check.sh`](tools/rfc-check.sh)
checks the header block (status, decision, dissent, named reviewers, decision
date) and walks the ledger table for rows whose "where it landed" cell is empty —
an objection that was heard and absorbed rather than resolved:

```bash
sh 03-the-rfc-loop/tools/rfc-check.sh                       # your RFC and ledger
sh 03-the-rfc-loop/tools/rfc-check.sh '' 03-the-rfc-loop/worked-example-ledger.md
grep -n "^\*\*Status:" artifacts/03-rfc/rfc-<slug>.md      # is the state actually current?
```

The two items the script cannot reach — whether the dissent line is one the
dissenter would call fair, and which direction a `Superseded by →` link points —
are in [`rubric.md`](rubric.md), along with what an empty ledger does and does not
mean.

If the doc lives in a repo rather than a doc tool, the state transitions are
commits, and their messages are Topic 6's problem — which is a nice accident,
because `git log` on an RFC file is the cheapest possible decision history.

## Predict, then record

| | Objections you expect | Objections received | Of those, how many would otherwise have surfaced at code review | Days from circulation to written decision |
|---|---|---|---|---|
| Predicted | | | | |
| Actual | | | | |

**What would mean the experiment is broken rather than your prediction wrong:**
if the ledger is empty, do not conclude the design is sound. Check whether you
circulated a doc describing code that already exists — reviewers read intent from
the tense you write in, and a doc describing something already built gets
rubber-stamped every time. Second break: the reviewer with no stake. Someone who
will never operate or extend the thing has no reason to fight you about it, and
their approval is politeness, not review. Third: check the calendar. If your
comment window overlapped a reviewer's on-call week, you did not measure their
opinion, you measured their pager.

## Answer before moving on

1. An objection is raised, you disagree, and you proceed anyway. Describe what
   must be in the document afterwards for this to be *better* than the objection
   never having been raised. (It is not obvious that it is better — argue it.)
2. Your ledger's most valuable row is the one whose objection you rejected. Why
   is that row worth more than the ones you accepted, given that the accepted
   ones changed the design and it did not?
3. The state field is four words and no mechanism enforces them. What actually
   makes `Superseded by →` get written, and what would you have to change about
   where docs live to make forgetting it harder than remembering it?
4. Suppose your team adopts this and, six months later, RFCs are circulated but
   nobody comments and everything is accepted unchanged. Give two structurally
   different diagnoses, and one observation that would distinguish them.

## Next up

[Topic 4 — the postmortem](../04-the-postmortem/README.md), the flagship of this
layer, and you have a live incident to write it about. The timeline is not the
analysis; the analysis is the answer to *why was this hard to see*.
