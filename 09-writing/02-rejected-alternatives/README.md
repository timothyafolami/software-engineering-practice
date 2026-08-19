# Layer 9 · Topic 2 — The rejected-alternatives section

### The takeaway (read this first)

**The one idea:** an alternative is only rejected honestly if you can state
**the condition under which it would have won** — and that condition is the
single most valuable sentence in the entire document, because it is the trigger
for revisiting the decision later.

**Why it matters in practice:** almost nobody writes this section, and most of
the people who do write it as theatre: three strawmen so obviously bad that the
reader's only available move is to agree. A real one does two things at once. It
proves you understood the problem space, and it hands the next engineer — often
you, eighteen months later — a tripwire. "We chose the in-process cache over
Redis because we run one instance; if we run more than one replica, this flips"
is a sentence that outlives your tenure.

**You'll know it landed when:** a reviewer argues that one of your rejection
conditions is *already true*.

## The concept

### Timeless reasons are the tell

The failure mode is easy to spot once it is named. Look at what the rejection
reason is a property *of*.

- A **strawman** rejection is a property of the alternative itself: "Redis adds
  an operational dependency."
- A **real** rejection is a property of *your current situation*: "Redis adds an
  operational dependency **and we run one instance, so the cache-coherence
  problem it solves does not exist for us yet**."

The first sentence was true in 2015, is true today, and will be true when your
company has four hundred engineers. That is exactly why it is useless: it cannot
distinguish the decision you are making now from any other decision anyone has
ever made about Redis. The second sentence has an expiry date and states it.

You can derive the whole discipline from that observation. If the rejection
reason contains no number from your system, no constraint with your team's name
on it, and no fact that could change next quarter, it is not a reason — it is a
category judgement about the technology, and you could have written it without
reading the problem.

### The condition is the artifact, not the rejection

Reframe what you are producing. The output of this section is not "we said no to
three things." It is a set of **named tripwires**, each attached to an
alternative that is currently losing and might later win. That reframing changes
what you write: you stop trying to make the alternatives look bad and start
trying to describe, precisely, the world in which each one becomes correct.

This is also the only mechanism by which a design decision can be *un-made*
safely. Without it, revisiting a decision requires someone to reconstruct the
original reasoning from scratch, and the usual outcome is that nobody does — the
decision calcifies not because it is still right but because the cost of
re-litigating it is unbounded.

### "Do nothing" is mandatory and is the hardest one

Every real design has a null alternative, and it is the one most likely to be
right. It is also the one people skip, because arguing against it honestly means
quantifying the harm of the status quo, and the status quo is the thing you have
already decided you hate. Force yourself: what happens if we ship nothing for six
months? If the honest answer is "it degrades slowly and nobody notices," you have
learned something more valuable than the design.

### Three processes that build this in structurally

- The **Rust RFC template** separates *Drawbacks* ("why should we not do this?")
  from *Rationale and alternatives*, so you cannot smuggle the case against your
  own proposal into the case against the alternatives. Two different sections,
  two different failure modes.
- **Kubernetes KEPs** keep an `Alternatives` section alongside `Risks and
  Mitigations` for the same separation.
- **Oxide's RFD process** — described in
  [RFD 1](https://rfd.shared.oxide.computer/rfd/0001), itself derived from the Go
  proposal process, the Rust RFC process, and Kubernetes proposals — treats the
  document as a durable artifact with an explicit lifecycle state. That is the
  part that makes a "revisit when X" tripwire actually reachable years later; a
  tripwire in a document nobody can find is decoration.

## How each language actually gets there

**This topic uses none.** The mechanism is a property of argument structure, not
of any runtime, and writing the same alternatives section six times would teach
nothing that writing it once does not.

The one place the language set does show up is in the *content* of a good
rejection, and it is worth naming because it is where Layer 1 pays for itself. A
rejection like "we are not rewriting the handler in Go" is a category judgement.
A rejection like "we are not rewriting the handler in Go — the blocking outbound
call would stop being a problem because the netpoller parks the goroutine rather
than the thread, but that buys us one endpoint's latency at the cost of a second
deployment toolchain, and it flips if more than `<n>` endpoints hit the same
pattern" is an argument. The difference is that the second one knows *why* Go
would have helped, which is a Layer 1 fact, not a Layer 9 one.

## The experiment

### The artifact

Take the design doc from [Topic 1](../01-the-design-doc/README.md) and write its
alternatives section **three times**. Keep all three in `artifacts/02-alternatives/`;
the diff between A and B is the lesson, and version C is the one that
occasionally changes the decision.

- **Version A** — as you naturally would, first instinct, no self-editing.
- **Version B** — rewrite so each alternative's rejection names a condition that
  would flip it. Aim for three alternatives, one of which must be **"do
  nothing"**.
- **Version C** — take the alternative you rejected most confidently and write
  two paragraphs as its strongest advocate, in good faith, with no hedging and
  no "of course, however." Then decide again.

Version C is uncomfortable by design. If it is easy, you are still writing as
yourself with a different hat on rather than as someone who actually holds the
position.

### Worked example

Numbers appear as placeholders. Fill them from your own measurements — a
rejection reason built on a number you did not measure is worse than no reason,
because it is persuasive.

Bad — timeless, unfalsifiable, strawman:

> **Alternative: increase the connection pool size.** Rejected — this just moves
> the bottleneck and doesn't fix the underlying problem.

Good — names the mechanism, the cost, and the flip condition:

> **Alternative: raise `pool_size` from `<current>` to `<larger>` and leave the
> handler synchronous.** This is one line and ships today. Rejected because the
> pool is not the binding constraint: at `<observed concurrency>` concurrent
> requests we are queueing on the outbound pricing call (measured: p99 of that
> call is `<your number>` for `<your share>` of requests, from `<source>`), and a
> larger pool converts a bounded wait into `<larger>` concurrent Postgres
> sessions on a server configured for `max_connections=<n>`, shared with the
> worker fleet. **This flips if we move pricing behind a timeout budget first** —
> with the outbound call capped at `<budget>`, the pool becomes the next
> constraint and raising it is the correct next change. Revisit after the timeout
> work lands.

Note what the good version costs you: it is now possible for a reviewer to say
"your p99 number came from a three-minute window during a deploy" and be right.
That is the point. It is also possible for the *next* engineer to notice that the
timeout work landed last quarter, which is the tripwire firing exactly as
designed.

And the mandatory one, done honestly:

> **Alternative: do nothing.** The endpoint is slow but not failing, and the
> complaint rate is `<measured>` per week from `<source>`. Rejected because the
> queueing is superlinear in arrival rate — at the growth we are seeing this
> degrades on its own rather than staying flat. **This flips if traffic growth
> stops**, or if the `<other work>` lands first and removes the arrival-rate
> pressure by itself.

### Rubric

1. Does every rejection reason contain at least one fact about *your* system — a
   number, a limit, a team constraint, a deployment fact?
2. Does every alternative have a stated flip condition, written as something that
   could be observed to be true?
3. Is "do nothing" present, and is its rejection reason something other than "the
   status quo is bad"?
4. Could you delete the name of your company from any rejection reason and have
   it still make sense? If yes, that reason is timeless and therefore empty.
5. In version C, is there a single hedge? Find and delete them, then reread.
6. Would a reviewer be able to argue that one of your flip conditions is already
   true? If not, your conditions are too far away to be useful.

## How to run

The three versions of this section for the checkout-latency decision are written
out in [`worked-example/`](worked-example/) — read A and B as a diff before you
draft your own, because the diff is the entire lesson:

```bash
cd 09-writing/02-rejected-alternatives/worked-example
diff -u version-a.md version-b.md | less
$EDITOR version-c.md          # the uncomfortable one, and what it changed
```

Then your own, in the workspace:

```bash
cd 09-writing/artifacts/02-alternatives
$EDITOR version-a.md version-b.md version-c.md
diff -u version-a.md version-b.md | less     # this diff is the lesson
```

Rubric items 1–3 are mechanical. [`tools/flip-check.sh`](tools/flip-check.sh)
walks each `**Alternative:` block and reports whether the rejection names a flip
condition and contains at least one fact from your own system, and whether "do
nothing" is present at all. Run it on both worked versions first — version A
fails every block, version B passes every block, and that contrast is the
calibration:

```bash
cd 09-writing
sh 02-rejected-alternatives/tools/flip-check.sh \
   02-rejected-alternatives/worked-example/version-a.md \
   02-rejected-alternatives/worked-example/version-b.md
sh 02-rejected-alternatives/tools/flip-check.sh          # then your own drafts
```

A quick check on rubric 4 — the script ends by pulling your rejection lines out
so you can read them with no surrounding context. Every line that survives should
still be recognisable as being about your system. Any line that reads like
general technology advice goes back. The subtraction test for that item, and the
two questions that make rubric 6 answerable, are in [`rubric.md`](rubric.md).

Then circulate version B with the doc from Topic 1 and log the row in
[`../log.md`](../log.md).

## Predict, then record

| | Alternative you expect to defend hardest | Will "do nothing" survive contact | Did version C change your decision |
|---|---|---|---|
| Predicted | | | |
| Actual | | | |

**What would mean the experiment is broken rather than your prediction wrong:**
if all three alternatives are rejected for reasons that would be equally true at
any company in any year, you have written Version A three times and the exercise
never ran. The tell is the absence of a number or a named constraint from *your*
system in the rejection reason. The second break is subtler: if version C was
easy to write and changed nothing, check whether you picked the alternative you
rejected most *confidently* or the one you rejected most *casually* — the
exercise only bites on the former.

## Answer before moving on

1. Pick your strongest rejection. What would have to become true for it to flip,
   and who in your organisation would notice that it had? If the answer is
   "nobody," the tripwire does not exist, whatever the document says.
2. "Do nothing" is the alternative most likely to be correct and least likely to
   be written. Give a concrete mechanism for that bias — what makes it hard to
   argue for the status quo in a document you are writing because you want to
   change something?
3. A rejection reason with a number in it can be attacked; one without cannot.
   Explain why "can be attacked" is the property you want, in a document whose
   nominal purpose is to get approval.
4. Eighteen months from now someone finds your doc and disagrees with the
   decision. What in the alternatives section makes it possible for them to
   change it *without* redoing your analysis — and what would they need that you
   did not write down?

## Next up

[Topic 3 — the RFC loop](../03-the-rfc-loop/README.md): a design doc without a
process is a diary. Named reviewers, a deadline, an explicit state, and a written
decision — and what to do with the objection that loses.
