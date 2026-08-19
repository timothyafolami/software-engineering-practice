# Layer 9 · Writing

> **You own this when:** someone reads your design doc and disagrees with a
> specific tradeoff rather than asking what you meant.

Read that test again, because it is sharper than it looks. It is not "people like
your doc." It is not "people approve it." It is: *the reader's objection is aimed
at a decision you made, not at a sentence you wrote.* That only happens when the
doc contains claims specific enough to be wrong. Most engineering writing fails
this test not because it is unclear but because it is **unfalsifiable** — it says
"this design is scalable and maintainable," which nobody can disagree with,
instead of "this design costs one extra network hop per request in exchange for
letting us deploy the two halves independently," which anybody can.

That is the whole layer in one sentence: **writing is the act of converting your
judgement into claims someone else can attack.** Each topic is a different venue.

| # | Topic | Folder |
|---|---|---|
| 1 | The design doc | [`01-the-design-doc/`](01-the-design-doc/README.md) |
| 2 | The rejected-alternatives section | [`02-rejected-alternatives/`](02-rejected-alternatives/README.md) |
| 3 | The RFC as a disagreement-extraction tool | [`03-the-rfc-loop/`](03-the-rfc-loop/README.md) |
| 4 | The postmortem — flagship, and you have a live one | [`04-the-postmortem/`](04-the-postmortem/README.md) |
| 5 | Explaining a tradeoff to someone who does not care how it works | [`05-explaining-tradeoffs/`](05-explaining-tradeoffs/README.md) |
| 6 | Commit messages and PR descriptions that explain why | [`06-commits-and-prs/`](06-commits-and-prs/README.md) |
| 7 | Writing publicly, one post a month | [`07-writing-publicly/`](07-writing-publicly/README.md) |

## Why this layer looks different from Layers 1–8, and why it isn't

The other layers end in a benchmark. This one ends in an artifact you wrote and
someone else read, which cannot honestly be faked into a benchmark. So the
structure survives with one substitution: where the other layers say *predict the
number, then measure it*, this layer says **predict the disagreement, then
circulate and record it.** Every topic ships the way the rest of the lab does — an
artifact, a rubric you score yourself against before sending, a **blank**
predict-then-record table, and a note on what would mean the exercise is broken
rather than your prediction wrong.

## The shared workspace

[`lab/`](lab/README.md) holds what all seven topics share: the `templates/`,
`artifacts/` and `log.md` layout, the column semantics for `log.md` (the
"specific disagreement?" column is the only score that matters), the two rules
that apply to every artifact here — never write a number you did not measure, and
make at least one claim specific enough to be wrong — and the sanitisation gate
that Topics 4 and 7 both run before anything leaves the building.

Topic 4 is a real postmortem of your live FastAPI/Postgres latency incident and,
where the evidence is missing, sends you to
[`06-observability/lab/`](../06-observability/lab/README.md) to go get it. Topic 1
proposes a fix you would genuinely ship for the same incident.

## On the language set

Six languages are available — Python, Node.js, Go, Rust, C++, Java — and this
layer mostly does not use them, which is correct rather than an economy: the
mechanism is a reader's model of a system and lives outside every runtime.

- **Topics 1 and 4 use Python only** — the artifacts are about your production
  FastAPI/Postgres service, and a runtime fact enters as *content*: "a
  synchronous call inside an `async def` holds the event-loop thread" is a Layer 1
  fact and one of the most useful sentences a contributing-factor list can carry.
- **Topics 2, 3 and 5 use none.** Argument structure, review process and audience
  conversion are language-independent.
- **Topic 6 uses all six**, genuinely: what a commit message must carry is
  universal, but CPython, npm, Go, rustc, LLVM and OpenJDK each put the reasoning
  in a different place — and each fails the same way when that place becomes
  unreachable.
- **Topic 7 uses all six indirectly** — the six runtimes generate the material
  that clears the AI-slop floor, and they clear it in different ways.

## A cadence, not a block

`SEQUENCE.md` does not schedule this layer as a block, and blocking it would
defeat it: Topic 6 from week one, Topic 1 once you have a candidate fix, Topic 4
at the end of Block D when you know the answer, one post a month from month two.

## The through-line

Every topic is the same move in a different venue: make a claim specific enough
to be wrong, give it to someone who might say so, record what came back. If you
do only one, do [Topic 4](04-the-postmortem/README.md) — you have a live
incident, the writeup is owed, and the `unknown` cells in that timeline are the
most honest engineering roadmap you will get this year.

## Next up

Layer 10 — ML systems, and inference as a memory-bandwidth problem. The roadmap's
plan has one design doc and one technical post published *every month throughout*
Layer 10; this layer is what makes that instruction survivable.
