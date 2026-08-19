# Layer 9 · Topic 1 — The design doc

### The takeaway (read this first)

**The one idea:** a design doc is not documentation of a decision, it is a
*device for relocating disagreement earlier in time* — and it works by
converting claims about your design from unfalsifiable ("clean," "scalable")
into falsifiable ("costs one extra network hop per request, buys independent
deploys of the two halves").

**Why it matters in practice:** the cost of discovering a wrong decision is
monotonic and brutal. In a doc it costs an afternoon. In code review it costs
the week you already spent plus the social friction of asking someone to throw
that week away. In production it costs an incident. The doc is not overhead you
pay for process; it is the cheapest place in the entire pipeline to be wrong.

**You'll know it landed when:** you can look at a paragraph of your own writing
and say "nobody could disagree with this sentence, therefore it is doing no
work," and delete it.

## The concept

### Falsifiability, derived rather than asserted

You do not need a rule for which sentences belong in a design doc. You can
derive one from a single test: **negate the sentence and ask whether anyone
would ever write the negation.**

> "This design is scalable and maintainable."

Negation: *this design is unscalable and unmaintainable.* Nobody has ever
written that sentence about their own proposal, so the original carries no
information — it does not distinguish your design from any other design. It is
grammatically a claim and informationally a noise floor.

> "This design costs one extra network hop per request and lets us deploy the
> two halves independently."

Negation: *it costs no extra hop, or it does not let us deploy independently.*
Both are positions a reasonable colleague might actually hold, and either could
be shown false by looking. The sentence is doing work.

Run that test over a draft and most of the adjectives disappear on their own.
What survives is the doc.

### The three jobs, and why the standard sections exist

A design doc has to do three things and only three: **establish the constraints
the reader does not have in their head**, **state what you chose**, and **expose
the seams where you might be wrong**. Every section in every template you have
ever seen maps onto one of those, which is why you can rebuild the template from
scratch if you lose it.

**Context** exists because your reader does not have your last three weeks. It
is the only section where you are allowed to be boring and the only one where
being incomplete is fatal — a reader missing a constraint will object to your
design for a reason you already ruled out, and you will both lose an hour to it.
The curse of knowledge is the whole difficulty here: the constraints most worth
writing down are precisely the ones that have become so obvious to you that they
feel like background.

**Goals and explicit non-goals** are where most docs quietly fail. A non-goal is
not "things we're not doing" as filler; it is a *pre-emptive scope defence*, and
each one should be something a reasonable reader would otherwise assume you were
doing. "Non-goal: fixing the N+1 on the list endpoint" earns its line precisely
because someone reading a latency doc will assume you are. Kubernetes makes
Goals/Non-Goals a required KEP field for this reason.

A goal that implies no measurement is a mood. "Improve checkout latency" tells
you nothing about whether you succeeded; "hold p99 on `POST /checkout` under the
threshold in Context at the current arrival rate" tells you what to graph.

**Proposed design** is the part you think is the doc. It isn't. It is the part
the reader skims to find the interfaces, the data model, and where it degrades.
Write the request path end to end, before and after — that one contrast usually
answers more reviewer questions than the prose around it.

**Alternatives considered** is the part that signals seniority, and it is big
enough to get [its own topic](../02-rejected-alternatives/README.md).

**Risks** and **open questions** differ in one way that matters: a risk is
something you have decided to accept, an open question is something you are
asking the reader to help decide. Filing something as a risk when it is actually
an open question is how you end up owning a decision you never meant to make
alone — and it is invisible at review time, because both look like prudence.

**Rollout and rollback** is where the doc stops being a think-piece. State the
rollback in units of time. "We can revert" is a wish; "revert is one flag flip,
under two minutes, no data migration to undo" is a plan, and the difference is
usually discovered while writing that sentence rather than after.

### Two canonical sources worth reading once, properly

- [Design Docs at Google](https://www.industrialempathy.com/posts/design-docs-at-google/)
  (Malte Ubl) — the best short argument for why the doc exists at all, and for
  the point that its value is mostly consumed *before* it is finished.
- The [Rust RFC template](https://github.com/rust-lang/rfcs/blob/master/0000-template.md)
  — still the best section list in the industry: Summary, Motivation,
  Guide-level explanation, Reference-level explanation, Drawbacks, **Rationale
  and alternatives**, Prior art, Unresolved questions, Future possibilities.
  Note that *Drawbacks* — "why should we **not** do this?" — is mandatory in
  that template and almost nobody who copies the template copies that section.

## How each language actually gets there

This is a prose layer, so the six-language treatment mostly does not apply, and
saying which languages are in play is more useful than pretending they all are.

**This topic uses one: Python.** The doc you write is about your production
FastAPI/Postgres service, and the design decision is a real one you would ship.
The mechanism a design doc operates on — a reader's model of a system, and the
constraints missing from it — lives entirely outside any runtime.

Where the language *does* re-enter is in the Context section, and it is worth
noticing: the constraints that belong there are frequently runtime facts you
learned in Layer 1. "The outbound pricing call is synchronous inside an
`async def`, so it occupies the single event-loop thread for its whole duration"
is a Python/asyncio property, it is falsifiable, and it is exactly the kind of
sentence a reader cannot supply for themselves. The same service written in Go
would not have that constraint and the doc would be a different doc — which is
the strongest argument in this layer for why the Layer 1 material is worth
having before you write.

## The experiment

The artifact *is* the experiment. There is no benchmark to run; the measurement
is what a named human sends back, and the prediction is what you think they will
send back.

### The artifact

A real design doc, two to four pages, for a change to your production Python
service that you would actually ship. The obvious candidate is the latency work:
pick **one** concrete intervention — a request timeout budget, moving the
synchronous outbound call off the event loop, resizing the connection pool,
adding the composite index — and write the doc as if the decision were live,
because it is.

Copy this into `templates/design-doc.md` (see
[`../lab/README.md`](../lab/README.md) for the workspace layout):

```markdown
# <Title: the change, not the problem>
Author · Date · Status: Draft | In review | Accepted | Superseded by <link>
Reviewers: <two names, at least one who will disagree>
Decision needed by: <date>

## Context
What is true today. The constraint that is not in the reader's head. Any number
you have measured, with how you measured it. No proposals in this section.

## Goals
Testable. Each goal should imply a way to tell whether the change worked.

## Non-goals
Things a reasonable reader would otherwise assume are in scope. One line each.

## Proposed design
Components, interfaces, data model, failure paths. Where it degrades and how.
What the request looks like end to end, before and after.

## Alternatives considered
Each: what it is, why it loses, and the condition under which it wins.

## Risks
Accepted downsides. For each: how you would notice it happening in production.

## Open questions
Things you want the reader to decide. Name the decider if you know them.

## Rollout and rollback
How it ships, behind what flag, what the rollback is, and how long rollback
takes.
```

### Worked example — the Context sentence

Placeholders are deliberate. Fill them from your own measurements; a number you
did not measure does not belong in a doc anyone will make a decision from.

Doing no work — true of every service ever written:

> Our checkout endpoint is slow under load and this is hurting the user
> experience. The architecture makes it hard to fix.

Doing work — three separate things a reviewer can contradict:

> `POST /checkout` calls the pricing service synchronously with
> `requests.post` from inside an `async def` handler, so that call holds the
> event-loop thread for its full duration. p99 on that outbound call is
> `<your number>` over `<window>`, from `<dashboard query or log range>`. The
> SQLAlchemy pool is configured `pool_size=<n>, max_overflow=<n>` against a
> Postgres server with `max_connections=<n>` shared with the worker fleet.

The second version costs you something, and that cost is the point: a reviewer
can now reply "your p99 is from a three-minute window during a deploy" and be
right. The first version is immune to that reply and therefore immune to review.

### Rubric — score yourself before you send

1. Can a reader find, in Context, every constraint they would need to evaluate
   the proposal without asking you a question?
2. Is there at least one sentence a competent engineer could disagree with on
   the merits? Underline it. If you cannot find one, the doc is not done.
3. Does every goal imply a measurement?
4. Is each non-goal something a reader would otherwise have assumed was in
   scope?
5. Do you state explicitly what you will *not* know until you ship?
6. Is the rollback described in units of time?
7. Word count of Context + Alternatives versus Proposed design. If the proposal
   dominates by more than 2:1, you have written a plan, not a design doc.

## How to run

The workspace and the template already exist — [`../lab/README.md`](../lab/README.md)
defines the layout, and `templates/design-doc.md` is the block above, written out.
Two files in this folder are worth reading before you draft:
[`worked-example.md`](worked-example.md), the same doc filled in for the checkout
latency decision with every number left as a placeholder, and
[`rubric.md`](rubric.md), the seven items above as checks you can run.

```bash
cd 09-writing
cp templates/design-doc.md artifacts/01-design-doc/<slug>.md
$EDITOR artifacts/01-design-doc/<slug>.md
```

Rubric 7 is mechanical, so measure it rather than eyeballing it. The script
buckets words by `## ` heading and prints the ratio; with no arguments it does
every draft in `artifacts/01-design-doc/`:

```bash
sh 01-the-design-doc/tools/section-balance.sh
sh 01-the-design-doc/tools/section-balance.sh 01-the-design-doc/worked-example.md
```

It is POSIX `sh` and `awk` — no GNU flags, and the macOS `awk` that ships on this
machine runs it unchanged. The rest of the rubric's mechanical checks are at the
bottom of [`rubric.md`](rubric.md).

Then circulate: send it to **two named people** with a decision date. One of them
should be someone you expect to push back — a doc circulated only to people who
will agree with it is a diary. If you genuinely have nobody, the fallback is a
delayed self-review: put the doc down for 48 hours, then read it twice, once as
the person who has to operate it at 3am and once as the person who has to pay
for it. Log the row in [`../log.md`](../log.md).

## Predict, then record

Fill the Predicted row before you send. Fill Actual after the responses land.

| | Which section draws the most comments | Strongest objection you expect | Did anyone disagree with a *tradeoff* by name |
|---|---|---|---|
| Predicted | | | |
| Actual | | | |

**What would mean the experiment is broken rather than your prediction wrong:**
zero responses is not agreement. Check three things before concluding anything
about the doc — did you name specific reviewers rather than a channel, did you
give a decision date, and did you ask for a decision at all? A doc with no
requested decision has no deadline pressure and reliably draws no disagreement
regardless of quality. Second break: if every comment is a wording fix, that is
not a good sign, it is the reader telling you they could not find a decision to
argue with. Third: a reviewer with no stake in the outcome — someone who will
never operate or extend the thing — has no reason to fight you, and their
approval measures politeness rather than soundness.

## Answer before moving on

1. Take the sentence you underlined for rubric 2 and negate it. Is the negation
   a position a competent colleague could hold? If not, you underlined the wrong
   sentence — and what does that tell you about the rest of the paragraph it
   lives in?
2. Name one thing in your doc that you filed as a **risk** which is really an
   **open question**. What does the mis-filing cost you specifically, and who
   ends up owning the decision by default?
3. Your Context section is the one most vulnerable to the curse of knowledge.
   Name a constraint you did *not* write down because it felt too obvious, then
   argue the case that a reviewer would have objected without it.
4. Suppose your reviewers approve the doc unchanged and the change ships and
   works. What, exactly, did the doc buy you — and can you distinguish that from
   the case where the doc bought you nothing and you were simply right?

## Next up

[Topic 2 — the rejected-alternatives section](../02-rejected-alternatives/README.md):
why "we rejected Redis because it adds an operational dependency" is a sentence
with no expiry date, and therefore no value.
