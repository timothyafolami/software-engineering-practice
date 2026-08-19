# Layer 9 · Topic 7 — Writing publicly, one post a month

### The takeaway (read this first)

**The one idea:** the marginal cost of competent explanatory prose has gone to
zero, so the only thing left that is scarce is a measurement you took yourself on
a system you actually operate — and you happen to be running a lab that
manufactures exactly that.

**Why it matters in practice:** the compounding is real but it is no longer about
volume. One post a month containing a number nobody else has is worth more than
twelve posts explaining concepts that a model will explain better, instantly, and
on demand.

**You'll know it landed when:** someone you have never met cites a number from
one of your posts back at you.

## The concept

### The slop floor, and what is above it

Generic explanatory prose is now free and infinite. That has vaporised the value
of the "here's what a connection pool is" post — not because such posts became
worse, but because the supply became unbounded and the reader has a better
substitute one prompt away.

What survives is firsthand: a number you measured on a machine you own, an
incident you were on call for, a thing that broke in a way the docs said it
would not. The test to apply to any draft is one question:

> *What is in here that could not have been generated without access to my
> machine, my production system, or my mistakes?*

If the answer is nothing, you have written something that is now free, and the
post will land accordingly. If the answer is "a benchmark on this hardware," "a
config that fails in this exact way in this version," or "the wrong hypothesis I
held for six hours during an incident," you have something.

### Why the negative result is the most valuable thing you own

The scarcest category is not the successful benchmark. It is the benchmark that
turned out to be measuring nothing, caught and reported by the person who
published it. Nobody writes those, because the incentive runs the other way, and
that is exactly why they carry information.

This repository contains one already, at no cost to you: Layer 1's results tables
were generated and never run, and its C++/Rust "no lost updates" figure turned out
to be the optimiser hoisting the increment out of the loop — rebuilt at `-O0`,
roughly 1.9 million lost updates appear. That claim is recorded, with its
provenance, in this lab's [root README](../../README.md) and
[`PREDICTIONS.md`](../../PREDICTIONS.md); if you publish it, rerun it yourself
first and publish *your* number, on *your* machine, with the compiler version.
That last sentence is the whole discipline of this topic.

### Ranked by scarcity, not by topic

1. **The sanitised latency postmortem from
   [Topic 4](../04-the-postmortem/README.md).** Highest value and hardest: real
   incident narratives with real numbers are among the rarest things on the
   technical internet and among the most read. Sanitisation gate first — see
   below.
2. **Layer 1's verification story.** "The benchmark I published was measuring
   nothing; here is how I caught it" is a genuinely good post, and almost nobody
   writes the negative result.
3. **Anything where the docs said one thing and your machine did another.** Layer
   1's macOS-versus-Linux failures qualify: code written against `sys/epoll.h` and
   `/proc` does not run on Darwin at all, which is a portability lesson with a
   reproducible failure attached.
4. **A prediction you logged and got wrong.** `PREDICTIONS.md` is a record of your
   own model being wrong in public, which is the same asset as the negative result
   in a smaller package.

### The cadence beats the ambition

One post a month, forever. Make it mechanical: **the last working day of the
month, publish whatever is closest to done.** The month's lab work is the raw
material, so the pipeline is already full, and the deadline is what converts a
folder of drafts into published work. A monthly cadence you keep produces more
than a weekly cadence you abandon in March, and the compounding only starts once
there is a body of work to compound.

### Sanitisation is a gate, not an edit

Run the checklist in [`../lab/README.md`](../lab/README.md) **before** you write,
not after. It is much harder to launder a finished draft than to write a clean
one, and the failure mode of laundering is that you remove the identifying detail
but keep the shape it was load-bearing for, ending up with a post that is both
unsafe and unconvincing. Employer sign-off in writing before publishing anything
derived from a production incident.

## How each language actually gets there

**This topic uses all six, indirectly** — not because a post has a language, but
because the six runtimes are what generate material that clears the slop floor,
and they clear it in different ways:

- **Python** — the production stack, so it is where the incident posts come from.
  Also the highest-noise area: everyone writes about Python, so the bar for a
  Python post is "a number from your own service," not an explanation.
- **Node.js** — the failure modes are famous and the *mechanism* is still widely
  misdescribed. A post that correctly separates "your JS is single-threaded" from
  "libuv runs a pool behind your back" clears the floor on precision alone.
- **Go** — usually the language that does *not* have the problem, which makes it
  the best control group in a comparative post. "Here is the same bug in four
  runtimes, and here is why one of them shrugs" is a shape that only exists if you
  ran all four.
- **Rust** — compile-time enforcement makes for a rare kind of post: the code you
  could not write, and what the borrow checker was protecting you from. Negative
  space is hard to generate and easy to verify.
- **C++** — the same hazards with no guardrails, and the only language here
  talking to the kernel with nothing in between. It is also where the optimiser
  quietly deletes your experiment, which is the source of the best post on this
  list.
- **Java** — virtual threads are recent enough that most published material is
  either marketing or a hello-world. A measured comparison against platform
  threads on your own hardware is scarce by default.

The general rule: the scarce thing is the *contrast you ran*, not the language you
ran it in.

## The experiment

### The artifact

One post, published, this month. Plus a row in `log.md`'s second table:

| Month | Post | The scarce thing in it | Where it came from |
|---|---|---|---|
| | | | |

If the "scarce thing" column is hard to fill, that is the finding, and the fix is
to go and run something rather than to write harder.

### Worked example — the same claim, above and below the floor

Below the floor. Free, infinite, indistinguishable from generated text:

> Connection pools are important because opening a connection is expensive. If
> your pool is too small, requests queue up and latency increases. You should
> size your pool based on your workload.

Above it. Specific enough to be wrong, and impossible to write without having run
something:

> On `<machine, OS, arch>` with `<Postgres version>` and `<driver version>`, a
> pool of `<n>` with `max_overflow=0` under an open-loop load of `<rate>` req/s
> produced `<your measured p99>` while `<n2>` produced `<your measured p99>`.
> Here is the k6 script and the compose file. The part I got wrong first: I
> measured with a fixed-VU generator, which self-throttles when the service slows,
> and so it could not reproduce queueing at all — the numbers above are from the
> rerun.

The second one invites a reader to say "your driver version has a known regression
in that path." That is not a risk to manage; it is the entire return on
publishing.

### Rubric

1. Is there at least one number in it that you personally measured, with the
   conditions stated — machine, version, load shape?
2. Could a reader reproduce your result, or at least the shape of it?
3. Does it include something that went wrong, or that you got wrong?
4. Is there a claim specific enough that someone could show up and prove you
   wrong? (Same test as [Topic 1](../01-the-design-doc/README.md). It is the same
   skill.)
5. Sanitisation gate run, before writing, with sign-off where required.
6. Does every number you did *not* measure carry its source?

## How to run

Rubric 5 comes first, before you write a word. The mechanical half of the gate is
scripted; the half it cannot check prints at the end of every run, and that is
the half that matters:

```bash
cd 09-writing
python3 lab/tools/sanitise_gate.py
cp lab/tools/sensitive-patterns.example.txt lab/tools/sensitive-patterns.txt
$EDITOR lab/tools/sensitive-patterns.txt      # your employer's names, hosts, customers
```

Then draft, from [`post-skeleton.md`](post-skeleton.md) — whose first field is
the scarcity line, deliberately, because it is the field that decides whether
there is a post at all:

```bash
cp 07-writing-publicly/post-skeleton.md artifacts/07-posts/<yyyy-mm>-<slug>.md
$EDITOR artifacts/07-posts/<yyyy-mm>-<slug>.md
```

[`worked-example.md`](worked-example.md) has the same claim written below and
above the floor, and then this lab's own material ranked by scarcity, with the
warning attached to item 2: a number recorded in this repository is not yours
until you rerun it.

Rubrics 1 and 6 have a cheap proxy —
[`tools/scarcity-check.sh`](tools/scarcity-check.sh) lists the conditions a
reader would need in order to check you (machine, architecture, versions, load
shape, build flags) and then every numeral in the draft, so you can answer one
question per line: did I measure this, or does it carry its source?

```bash
sh 07-writing-publicly/tools/scarcity-check.sh artifacts/07-posts/<yyyy-mm>-<slug>.md
python3 lab/tools/sanitise_gate.py artifacts/07-posts/<yyyy-mm>-<slug>.md   # again, on the final draft
```

Then distribution, which is a separate problem from writing and should not be
confused with it: send it directly to three engineers you know and ask "is there
anything here you would push back on?" That is also the measurement below — and
the only thing that tells you which kind of silence you got.

## Predict, then record

| Month | Post | Which claim you expect to be challenged | What was actually challenged |
|---|---|---|---|
| | | | |

**What would mean the experiment is broken rather than your prediction wrong:**
silence on a post is ambiguous. It could be reach, or it could be that there was
nothing to argue with, and those call for opposite responses. Distinguish them
with the three-engineer test above: if three people who read it closely find
nothing to challenge, the post is confirming things everyone already believed,
which is a content problem, not a distribution one. If they find plenty and the
internet finds nothing, you have a distribution problem and the writing is fine.

Second break: a post that draws heavy engagement on its opinions and none on its
numbers has not tested what you think it tested. Check whether the numbers were
actually load-bearing in the argument or were decoration around a take.

## Answer before moving on

1. Take your draft and delete every sentence that a model could have written
   without access to your machine. What is left? If it is under a paragraph, the
   post is that paragraph plus supporting material — restructure around it.
2. Publishing a negative result about your own work has an obvious cost and a
   non-obvious return. Name the return precisely, in terms of who reads it and
   what it changes about how they treat your positive results.
3. You have a measured number that is genuinely interesting and genuinely
   sensitive. Give two ways to publish the *finding* without the absolute
   number — and say what each one costs the reader's ability to check you.
4. The scarcity argument says firsthand measurement is what survives. What is the
   strongest case *against* that claim — what kind of non-firsthand writing still
   clears the floor, and why?

## Next up

That is the layer. Back to [the index](../README.md) for the through-line, and
then Layer 10 — where the roadmap's own plan has one design doc and one technical
post published *every month throughout*, which is what this layer exists to make
survivable.
