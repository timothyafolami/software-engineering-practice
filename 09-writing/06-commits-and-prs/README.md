# Layer 9 · Topic 6 — Commit messages and PR descriptions that explain why

### The takeaway (read this first)

**The one idea:** the diff is a complete, perfectly compressed record of *what*
changed; the *why* is the only information that writing the code destroys, and
the commit message is the last place it can be preserved.

**Why it matters in practice:** every hour you have ever spent staring at a line
of code asking "why on earth is this here" is an hour someone else could have
saved you with two sentences. And the argument got stronger recently, not weaker:
agents write competent diff-summaries for free, so the *what* is now automated and
the *why* is the entire remaining human contribution to the record.

**You'll know it landed when:** you can `git log` your own repo from a year ago
and recover a constraint you had completely forgotten.

## The concept

### Four things belong in a message, and only one of them is in the diff

1. **Why now** — the trigger. An incident, a report, a measurement. This is the
   one that dates the decision, which is what makes it possible to notice later
   that the trigger no longer applies.
2. **What you rejected** — the approach you tried first and abandoned. The
   highest value-per-word sentence in software archaeology, because it is the
   only thing that stops the next person walking your dead end. Almost nobody
   writes it.
3. **The constraint** — the non-obvious reason the code is shaped this way.
   "Must stay idempotent, the caller retries." "Ordering matters here, the index
   is on `(customer_id, created_at)`."
4. **How you know it works** — what you measured or ran.

Only the third is even partially recoverable from the code, and only if the
reader already knows to look.

### Why the *why* is the scarce input now

GitHub's guidance on reviewing agent-authored PRs makes the point from the
reviewer's side: the **linked issue body — the intent — is the most underused
context input in review workflows**
([*Agent pull requests are everywhere. Here's how to review
them.*](https://github.blog/ai-and-ml/generative-ai/agent-pull-requests-are-everywhere-heres-how-to-review-them/)).
Read together with the fact that diff-summarisation is now free, that gives a
clean division: the machine can tell you what changed and can often tell you
whether it is internally consistent; only a human can tell you what problem it
was supposed to solve, which alternatives were live, and what constraint the
shape of the code encodes.

The failure mode this creates is new and worth naming: a PR body that restates
the diff in fluent prose *looks* like diligence and carries zero information. If
an agent wrote the body, your job is not to check its grammar — it is to replace
the summary with the reasoning only you have.

### Trailers: the machine-readable slice

Git has a native mechanism for the structured part. `Fixes: #123`, `Refs:`,
`Co-authored-by:` are greppable, survive rebases, and are understood by tooling.
Use trailers for links and prose for reasoning; they are not substitutes for each
other.

If your team uses Conventional Commits, fine — the `feat:`/`fix:` prefix is a
changelog-generation feature, not a substitute for explanation. A `fix:` with an
empty body is still an empty commit message, and it is worse than "fix bug"
because it looks compliant.

### The archaeology test is the only honest measure

You cannot evaluate your own commit messages by reading them; you wrote them, so
you supply the missing context for free. The only real test is whether the
history answers a question for someone who does not already know the answer —
which is why the experiment below is a timed retrieval task against old code
rather than a review of your recent messages.

## How each language actually gets there

**This topic uses all six**, and it is the one place in this layer where that is
not padding: the *mechanism* (a message carries what the diff destroys) is
universal, but the conventions you have absorbed come from whichever ecosystem
you read most, and they differ enough to change what you write by default.

- **Python** — no ecosystem-mandated commit format; CPython itself uses
  `gh-NNNNN: summary` tied to the issue tracker, and the reasoning lives mostly in
  the linked issue. Consequence for you: in a Python codebase the commit body is
  frequently *empty by convention*, and the constraint is in a tracker you may not
  still be paying for. Write the constraint into the commit.
- **Node.js** — the npm ecosystem is where Conventional Commits took hold, driven
  by automated semantic-versioning and changelog tooling. Consequence: the message
  format is machine-load-bearing, which pulls attention toward the prefix and away
  from the body. The prefix is for the release notes; the body is for the human.
- **Go** — the Go project's convention is `package: short summary`, with the
  reasoning in the body and review happening on Gerrit changes rather than
  branches. Consequence: Go's culture assumes one logical change per commit, which
  is the discipline that makes `git log -L` useful at all.
- **Rust** — the language's decisions live in RFCs, so a rustc commit is often the
  *implementation* of an argument recorded elsewhere, linked by number.
  Consequence: the model to steal is the link from the change to the durable
  argument — exactly the loop [Topic 3](../03-the-rfc-loop/README.md) builds.
- **C++** — large C++ codebases (LLVM, Chromium) have some of the strictest commit
  hygiene anywhere, because the code is long-lived, the invariants are unwritable
  in the type system, and a subtle change in a header is invisible in review.
  Consequence: in a language where the compiler cannot record the constraint, the
  commit message is the only place it can live. This is the sharpest version of
  the whole topic.
- **Java** — OpenJDK ties commits to JBS issue keys and JEPs, so the message is a
  join key into a formal process. Consequence: it is the ecosystem most likely to
  have the reasoning written down *somewhere*, and most likely to have it
  somewhere you cannot reach in two years.

Two things worth extracting from the contrast. First, the ecosystems whose
compilers enforce the least (Python, C++, in opposite directions) put the most
weight on the message. Second, every convention above is about *where the
reasoning lives*, not whether it is needed — and every one of them fails the same
way, when the external system holding the reasoning becomes unreachable and the
repository is all that is left.

## The experiment

This one is genuinely mechanical, and it produces a number you can actually
measure — the only topic in this layer where that is true.

### The artifact — archaeology, on your own repo

Pick **three lines** in your production service that look arbitrary: a magic
number, a retry count, a defensive `if`, a `# noqa`. Pick them from code at least
six months old and, if possible, not written by you. For each line:

```bash
git log -L <start>,<end>:<path>          # every commit that touched those lines
git log --format='%H %s%n%b' -1 <sha>    # the full message, body included
```

Start a timer. Try to recover *why the line exists* from the commit history alone
— no asking anyone, no reading surrounding code. Record seconds spent and whether
you succeeded. Do all three, then compute the fraction where the history answered
the question.

Then the forward half: write your next **ten commits** and one real PR
description to the template below, and re-run the same archaeology against them a
month from now. That second run is the actual experiment; the first is the
baseline.

`templates/pr-description.md`:

```markdown
## Why
The trigger. What was observed, by whom, when. Link the incident, ticket, or the
postmortem section this came from.

## What changes
One paragraph. The diff has the rest.

## What I tried first and rejected
The approach that didn't work, and why. Do not skip this because it feels like
admitting something.

## Risk and rollback
What breaks if this is wrong, how it's noticed, how it's reverted, how long that
takes.

## How this was verified
Test, benchmark, load run, staging soak. Numbers you actually observed. If it was
only manually smoke-tested, say that.
```

### Worked example

A message that passes review and fails archaeology:

> ```
> fix: increase retry count
>
> Increases the retry count from 2 to 5 in the pricing client to make the
> integration more reliable.
> ```

The body restates the diff. A reader in eighteen months learns nothing they could
not have read from the change itself, and — worse — has no way to know whether 5
was reasoned or guessed.

The same change, carrying what the diff destroys:

> ```
> pricing client: retry 5x with jitter, not 2x
>
> Why now: <incident/ticket ref> — checkout failures during the pricing
> service's deploy window. Their rolling restart drops connections for
> <duration measured from: source>, and 2 retries at <backoff> did not span it.
>
> Rejected first: raising the client timeout instead. That holds the event-loop
> thread for the whole window and turns a fast failure into a slow one for
> every concurrent request (see <postmortem link>).
>
> Constraint: the pricing call must stay idempotent-safe to retry — it is a
> pure quote lookup today, and this commit is only correct while that is true.
>
> Verified: <what you ran, and what you observed>.
>
> Refs: <issue>
> ```

The third paragraph is the one that matters most and is easiest to leave out. It
is an invariant that no type system in this lab's six languages can express, and
the day someone makes that call non-idempotent, this commit is the only warning
in the repository.

### Rubric

1. Does the subject line say what the *change* does, not what file it touches?
2. Does the body answer "why now"?
3. Is there a rejected approach in it? (Aim: at least 3 of every 10 commits.)
4. Would a stranger with `git blame` and no access to you get their question
   answered?
5. If an agent wrote the body, did you replace the summary of the diff with the
   reasoning only you have? Restating the diff in prose is the failure mode, and
   it looks like diligence.
6. Does every number in the message say where it came from?

## How to run

Two runs: the baseline today, the same procedure again in a month.

```bash
cd <your production service repo>
LAYER9=<path to this lab>/09-writing/06-commits-and-prs

# BASELINE -- crude proxies over the last 50 commits, plus the churn hot list
sh $LAYER9/tools/archaeology.sh

# RETRIEVAL -- one line at a time, timed. History only: no surrounding code,
# no asking anyone, no tracker unless the message names it.
sh $LAYER9/tools/archaeology.sh app/pricing.py 120 124
```

[`tools/archaeology.sh`](tools/archaeology.sh) counts per *commit* rather than
per line — commits with a non-empty body, commits naming a rejected approach,
commits naming a constraint, commits carrying a machine-readable trailer — and
then lists the files with the most churn, which is the best hunting ground for
candidate lines. Both proxies are crude, and that is fine: you are measuring your
own change over a month, so a consistent crude proxy beats a precise one you will
not re-run. Record today's numbers in the second table below before you change
any habits.

**The forward half.** `templates/pr-description.md` is the block above, written
out; [`worked-example.md`](worked-example.md) fills it in for the change from
Topic 1, with the matching commit message underneath so you can see what the PR
body may lean on and the commit may not.

**Your ecosystem's version.** The six folders here are not the same page six
times — each names where that ecosystem puts the reasoning, how it fails when
that place becomes unreachable, and the hunting grounds worth grepping for
candidate lines: [`python/`](python/commit-conventions.md),
[`nodejs/`](nodejs/commit-conventions.md), [`golang/`](golang/commit-conventions.md),
[`rust/`](rust/commit-conventions.md), [`cpp/`](cpp/commit-conventions.md),
[`java/`](java/commit-conventions.md). Each carries a `commit-template.txt`
shaped for that ecosystem, and installing one is the highest-leverage habit
change in this topic because it moves the prompt to the moment you are already
writing:

```bash
git config commit.template $LAYER9/python/commit-template.txt
```

The ranked version of the four things — what to write first when you have thirty
seconds — is in [`rubric.md`](rubric.md).

## Predict, then record

| | Fraction of the 3 archaeology attempts you expect to succeed | Actual | Median seconds per attempt |
|---|---|---|---|
| Predicted | | | |
| Actual | | | |

Baseline versus one month later, same repo, same procedure:

| | Non-empty bodies in last 50 | Commits naming a rejected approach | Archaeology success on new lines |
|---|---|---|---|
| Today | | | |
| In one month | | | |

**What would mean the experiment is broken rather than your prediction wrong:**
if you succeed on all three instantly, you probably picked lines you wrote
recently — the test requires code old enough that you are genuinely retrieving
rather than remembering, ideally someone else's. If you fail on all three in
under ten seconds because the messages say "fix bug" and "update", that is not a
broken experiment, that is the result, and it is the most motivating one
available. A third break: if the repo squash-merges every PR, `git log -L` will
land you on a squash commit whose message is a PR title, and you are measuring
your PR descriptions rather than your commits — still a valid measurement, but of
a different thing, so say which one you measured.

## Answer before moving on

1. Of the four things that belong in a message, exactly one is partially
   recoverable from the code. Which, and how would a reader recover it — and what
   does that tell you about which of the four to write first when you are in a
   hurry?
2. An agent writes your PR body from the diff and it is accurate, complete, and
   well-written. Explain precisely what is still missing, in a way that does not
   reduce to "it wasn't written by a human."
3. Your team squash-merges, so one PR becomes one commit. Which of the four
   things gets lost, and where does it have to move to survive?
4. You wrote "constraint: must stay idempotent, the caller retries." Two years
   later someone makes it non-idempotent. Your commit message did not stop them.
   Was the message therefore worthless? If not, what did it buy — and what
   mechanism would have actually stopped them?

## Next up

[Topic 7 — writing publicly, one post a month](../07-writing-publicly/README.md):
the same claim-that-can-be-wrong, aimed at people you will never meet, in a world
where competent explanatory prose costs nothing.
