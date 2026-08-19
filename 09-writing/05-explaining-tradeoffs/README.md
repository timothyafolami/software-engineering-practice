# Layer 9 · Topic 5 — Explaining a tradeoff to someone who does not care how it works

### The takeaway (read this first)

**The one idea:** lead with the decision and its cost in *their* units — money,
time, or risk — because their decision function does not accept milliseconds as
an input type. The mechanism comes last, and only if they ask.

**Why it matters in practice:** this is the specific skill that determines
whether the latency work gets two weeks of your time or gets deferred behind a
feature. Not whether the work is correct. Whether it is legible to the person
allocating.

**You'll know it landed when:** a non-engineer restates your tradeoff back to
you, correctly, including what it costs — without using any of your nouns.

## The concept

### Why the natural order is exactly backwards

Engineers explain in the order they discovered things: mechanism, then
implication, then ask. That order is honest and it is also the order most likely
to lose, for a reason that has nothing to do with the audience's intelligence.

Your reader is triaging your paragraph against eleven others and will stop
reading at the first point where they can. If the decision is in sentence four,
they never reach it; they reach sentence two, classify the message as "technical
detail, will read later", and it dies. Put the decision in sentence one, the cost
in sentence two, and what you need from them in sentence three, and the message
survives triage even when it is read badly — which is the condition you should
design for, because it is the normal condition.

This is the same instinct behind Amazon's narrative memo culture. The six-pager
and the PR/FAQ exist because a document forces a coherent argument in a way
bullets do not, and it is read in silence at the top of the meeting so that the
argument gets *evaluated* rather than *performed*. You are doing a compressed
version of the same thing.

### The conversion step is the actual skill

p99 latency is not a unit anyone outside engineering owns. Neither is throughput,
nor pool size, nor error rate. Converting is a real skill and it has two failure
modes on either side of it.

- **"Checkout takes over three seconds for one in twenty customers"** is the same
  fact as a p99 number, in a unit the business owns. Good conversion: same fact,
  different unit.
- **"This is costing us `<invented>` in lost revenue per month"** is a bad
  conversion, because you do not have that number and the person you are talking
  to can tell. Invented precision does not survive scrutiny and it costs you the
  next conversation as well as this one.
- **"I can't tell you the revenue impact. Here is what I can tell you: `<the
  measured thing>`, and here is the number I would need to answer your question"**
  is hedged honesty, and it survives scrutiny. It also frequently gets you the
  missing data, because the person who owns the revenue number is usually in the
  room.

The general rule falls out of the same falsifiability test from
[Topic 1](../01-the-design-doc/README.md): the conversion must preserve the truth
value of the claim. If your conversion says something you have not measured, it
is not a conversion, it is a new and unsupported claim.

### The cost of not doing it, stated honestly

Every allocation request has an implicit alternative — spend the time on
something else — and the person deciding is comparing against it whether or not
you help them. Stating the cost of waiting is what makes the comparison possible.
Stating it honestly, including "it may stay about the same," is what makes you
someone they believe next quarter. There is an obvious short-term incentive to
imply catastrophe, and it works exactly once.

### The restatement test

The only reliable check on whether an explanation landed is to have the other
person say it back. Not "does that make sense?" — that reliably returns `yes`.
"What would you tell your own manager this is about?" returns the truth, and the
gap between what you said and what comes back is the entire measurement.

## How each language actually gets there

**This topic uses none, and that is the point of the topic.** The whole exercise
is removing implementation from the explanation. `p99`, `event loop`,
`connection pool`, `async`, `goroutine`, `GIL` — all of them are convertible, and
every one you keep is a bet that your reader will spend effort decoding it.

There is one asymmetry worth knowing. The mechanism sentence you offer at the end
— the one-line "short version" — is easier to write well if you genuinely
understand the runtime, because a correct simplification and a wrong
simplification look identical to the reader and only one of them survives a
follow-up question. "One of our internal calls blocks everything else while it
waits" is a correct simplification of the asyncio failure mode from Layer 1. "The
server runs out of memory under load" would have been a comfortable-sounding lie,
and the cost of it arrives later, when someone acts on it.

## The experiment

### The artifact

One page, plus a three-sentence version, for the latency decision — written for a
**real named person** who is not an engineer. Store both in
`artifacts/05-tradeoff/`.

Then the test: send the three-sentence version to that person and ask them to
tell you what you are recommending and what it costs. **Record what they say back
verbatim**, not your summary of it. Your summary will unconsciously repair their
misunderstanding, which is precisely the data you are trying to collect.

### Worked example

Engineer-native, and it will lose the argument:

> Our p99 latency is `<your number>` because we're doing a blocking HTTP call
> inside an async handler, which stalls the event loop. I want to refactor to an
> async client with a timeout budget and add a circuit breaker.

Same content, ordered for the reader. Placeholders are yours to fill from
measurement — an invented number here is worse than no number, because this is
the version someone will repeat in a meeting you are not in:

> **I want to spend `<n>` weeks fixing checkout slowness, starting next sprint.**
> Right now about `<your measured share>` of checkouts take more than
> `<threshold>` seconds, and it gets worse as we add traffic — this is the thing
> behind the "site feels slow" tickets from the last month. **The cost is `<n>`
> weeks of my time and one deploy with a rollback plan; the risk of waiting is
> that it degrades further on its own rather than staying where it is.** I need a
> yes or no by `<date>` to fit it into the sprint. (Happy to go into the technical
> detail if useful — short version, one of our internal calls blocks everything
> else while it waits.)

Note what changed and what did not. The facts are identical. The order is
inverted, the units are theirs, there is a date, and the mechanism has been
demoted to a parenthetical with an offer attached. Nothing was dumbed down; one
sentence was made available on request instead of mandatory.

### Rubric

1. Decision in sentence one. Not context. Not background. The decision.
2. Cost in money, time, or risk — never in a technical unit.
3. A specific thing you need, with a date.
4. Mechanism appears at most once, at the end, in one sentence, with an offer to
   expand.
5. Zero jargon that survives a find-and-replace with a plain phrase.
6. The cost of *not* doing it is stated, and stated honestly, including "it may
   stay about the same" if that is the truth.
7. Every number is one you measured, and you can say where it came from if asked
   — because you will be asked, and this is the room where you cannot check.

## How to run

Both versions are written out in [`worked-example/`](worked-example/) —
[`three-sentences.md`](worked-example/three-sentences.md) is the one that gets
forwarded, and [`one-pager.md`](worked-example/one-pager.md) is the one you send
when they reply. Both pass the jargon check with zero hits, which is the
calibration: it is achievable, and the facts survive it.

```bash
cd 09-writing
$EDITOR artifacts/05-tradeoff/one-pager.md artifacts/05-tradeoff/three-sentences.md
cp 05-explaining-tradeoffs/restatement-form.md artifacts/05-tradeoff/restatement.md
```

Rubric 5 is mechanical. [`jargon-list.txt`](jargon-list.txt) holds the terms as
`regex :: what to say instead`, so each hit arrives with its conversion rather
than a scolding; [`tools/jargon-check.sh`](tools/jargon-check.sh) runs it:

```bash
cd 09-writing/05-explaining-tradeoffs
sh tools/jargon-check.sh worked-example/three-sentences.md   # zero hits, on purpose
sh tools/jargon-check.sh                                     # then your own drafts
```

Add your own domain's terms to the list. It is only useful if it contains the
words *you* reach for, and the test for adding a word is not "is it technical"
but "does my reader own this unit".

Then send it, and ask the two questions that produce data rather than agreement:

1. "What would you tell your own manager this is about?"
2. "What is the strongest argument against doing this now?"

Write both answers down **verbatim** — in
[`restatement-form.md`](restatement-form.md), copied to
`artifacts/05-tradeoff/restatement.md`, in the record table below, and in
[`../log.md`](../log.md). Not your summary of what they said: your summary will
repair their misunderstanding, and their misunderstanding is the measurement.

## Predict, then record

| | What you expect them to ask first | What they actually asked | Did they restate the cost correctly |
|---|---|---|---|
| Predicted | | | |
| Actual | | | |

Verbatim restatement (fill after the conversation; do not paraphrase):

> 

**What would mean the experiment is broken rather than your prediction wrong:**
"sounds good, go ahead" with no questions is not success. It usually means they
did not understand the cost and are deferring to you, which means you have the
approval but not the buy-in — and you will discover the difference when those two
weeks show up on a status report. Push once with question 2 above; if they cannot
produce an argument against, they have not got the tradeoff and the exercise has
not run yet.

The second break is the audience. If you sent it to an engineering manager who
already agrees with you, you measured nothing: the conversion step was never
exercised, because they can decode the jargon version. The person has to be
someone for whom "p99" is genuinely not a unit.

## Answer before moving on

1. Take your strongest technical argument and convert it. Then check the
   conversion the hard way: is there any world in which the converted sentence is
   true and the original is false, or vice versa? If so, you did not convert, you
   substituted.
2. You do not have a revenue number and you are asked for one. Write the exact
   sentence you would say. Now write the version that invents it, and describe
   concretely what it costs you three months later.
3. Your reader restates your tradeoff and gets the cost right but the mechanism
   wrong. Is that a success or a failure? Defend your answer — and say what would
   change it.
4. Why does the decision have to be in sentence one, given that it is
   incomprehensible without the context in sentence two? (There is a real answer
   about how the message gets read, not just an assertion about attention spans.)

## Next up

[Topic 6 — commit messages and PR descriptions that explain why](../06-commits-and-prs/README.md):
the same move, aimed at a reader who is you, eighteen months from now, with no
memory and no way to ask.
