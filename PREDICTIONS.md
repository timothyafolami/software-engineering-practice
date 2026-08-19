# The prediction log

This is the most valuable file in the repo, and it is one table.

Everything else here — 79 topic READMEs, a compose harness, several hundred
lines of load-test config — is scaffolding for one question, asked before
each experiment and answered honestly after it:

> **What number do you expect, and were you right?**

Reading an explanation gives you the feeling of understanding. Predicting a
number and being wrong is the only thing that reliably tells you that you
didn't. This file is the difference between having read the lab and having
learned from it.

## The rule

**Before you run anything, write one row. It takes under a minute.**

If it takes longer than a minute you will stop doing it, and if you stop
doing it the lab degrades back into reading. So the format is deliberately
small, the prose is optional, and there is no ceremony.

## Format

One row per experiment. Append to the bottom. Never edit a prediction after
you have seen output — if you catch yourself doing that, the row is a miss
by definition.

```
| Date | Experiment | Prediction | Actual | H/M/B | What I got wrong |
```

| Field | Rule |
|---|---|
| **Date** | `YYYY-MM-DD`. |
| **Experiment** | Path fragment, e.g. `03-data/02-mvcc`. Enough to find it again. |
| **Prediction** | **Must be falsifiable.** A number with a unit, a ratio, or an ordering. "Slower" is not a prediction; "3-8x slower" is. A range is fine and honest — a wide range is a confession that you don't have the mechanism yet, which is useful information. |
| **Actual** | The measured number, same units. |
| **H/M/B** | **H**it, **M**iss, or **B**roken. |
| **What I got wrong** | Name a **mechanism**, not a number. "Underestimated by 4x" is worthless. "I forgot the pool blocks rather than fails, so the wait shows up as latency not errors" is the whole point of the exercise. Leave blank on a hit. |

### Hit, miss, broken

- **Hit** — the actual falls inside your stated range, *and* the mechanism
  you named is the one that produced it. Right number for the wrong reason
  is a miss. Log it as a miss. You will not remember which was which in six
  months, and the one you learn from is the miss.
- **Miss** — wrong direction, outside your range, or right number via a
  mechanism you didn't name.
- **Broken** — the experiment failed its own sanity check. **This counts as
  neither.** Fix the experiment and re-predict from scratch; do not adjust
  your prediction to match a run you don't trust.

Every topic README carries a "what would mean the experiment is broken
rather than the prediction wrong" note. Read it before you write **M**. The
usual culprits, in rough order of frequency:

- Benchmark ran for under ~10ms, so you measured the OS scheduler, GC, or
  JIT rather than your code.
- Load generator was closed-loop (fixed VUs), which self-throttles when the
  service slows and therefore cannot build a queue.
- Compiler optimized away the thing you were trying to observe (check at
  `-O0`, or check the disassembly).
- Working set fit in cache / the pool never actually saturated / the fault
  injector wasn't wired in.
- The code path you think you exercised was never reached (add a counter and
  assert it moved).

## Why hit-rate over time is the only real evidence

A layer landed when you can predict its behaviour, not when you finished
reading it. So track two things, per layer:

**1. Hit rate.** Roughly 30-50% early in a layer is normal and healthy. Above
~70% across a layer's experiments means the mechanism is genuinely internalized.
A hit rate near 100% means your predictions are too vague to be wrong — tighten
the ranges.

**2. The shape of your misses — this is the better signal.** Misses come in
two kinds:

- **Direction errors** ("I said the fix would help; it hurt"). You do not
  have the mechanism. Go back to `## The concept`.
- **Magnitude errors** ("right direction, off by 5x"). You have the
  mechanism and lack calibration, which fixes itself with reps.

**A layer has landed when your misses stop being direction errors.** That
transition is more meaningful than the raw percentage, and it is why the
"what I got wrong" column exists — it is the only column that lets you tell
the two apart later.

Review the log at the end of each layer. Ten minutes, once per layer. Count
H/M/B, count direction-vs-magnitude misses, and write two sentences at the
bottom of this file about what your misses had in common. They usually have
something in common.

## Worked example

Here is what a filled-in row looks like, from the connection-pool experiment
in Layer 5:

| Date | Experiment | Prediction | Actual | H/M/B | What I got wrong |
|---|---|---|---|---|---|
| 2026-08-24 | `05-failure/01-littles-law` | Knee at ~85-90% utilization; p99 rises smoothly ~2x from 50%→90% offered load, then goes vertical | Knee at 78%; p99 went 34ms → 41ms → 2.9s across 50/70/85% | **M** | Direction right, shape wrong. I predicted a smooth rise because I was thinking about M/M/1 with constant service time. Service time here has high variance (some requests hit the cache, some do a 40ms query), and Kingman's formula puts the queueing term proportional to (Ca²+Cs²)/2 — so variance moved the knee left and made the transition sharper than 1/(1−ρ) alone predicts. Also: p99 barely moved right up to the knee, which means **watching p99 is not an early warning**. Utilization and queue depth are. |

That row took ninety seconds to write and is worth more than the topic
README it came from, because it is the specific thing *this* reader did not
know. The next row on a queueing experiment will be better because of it.

## The log

Seeded with four entries that are already recorded as misses. These are
**not** predictions this reader got wrong — they are results the lab itself
published wrongly, before any verification pass. They are logged first, and
deliberately, because the lesson they encode is the one this whole file
exists to enforce: **generating the material is the cheap half; verifying it
is the half that counts.** Layer 1 skipped the second half and shipped four
false findings that read exactly like true ones.

Re-run each of these yourself and append a *real* row underneath. Until then,
the honest state of Layer 1 is "unknown."

| Date | Experiment | Prediction | Actual | H/M/B | What I got wrong |
|---|---|---|---|---|---|
| 2026-08-17 | `01-machine/01-memory-locality` | Published table claims 8.7x (Python) to 65x (Rust) sequential-vs-shuffled slowdown across six languages | **Never measured.** Table was LLM-generated, not produced by running the benchmark on this machine | **B** | The failure is not that the numbers are implausible — they are plausible, which is what made them dangerous. The failure is that a fabricated table is indistinguishable from a measured one once it's in a README. Fix: no results table in this repo ships pre-filled, ever. |
| 2026-08-17 | `01-machine/03-concurrency-models` | Go's "call-free tight loop" will keep running under `GOMAXPROCS=1` and still let other goroutines schedule, demonstrating asynchronous preemption (Go 1.14+) | **The experiment cannot show this.** The loop calls `time.Now()` every iteration, so it yields at function-call boundaries and would have behaved identically under the pre-1.14 cooperative scheduler | **B** | Wrote a test for a claim, then wrote a loop that violated the test's one precondition, then reported the expected result. A genuinely call-free loop is `for i := 0; i < N; i++ { x += i }` with no calls in the body and the result consumed afterward so it isn't elided. Re-run with `GODEBUG=asyncpreemptoff=1` as the control — if both variants behave the same, the loop still isn't call-free. |
| 2026-08-17 | `01-machine/04-races-and-atomicity` | C++ and Rust report **0 lost updates** on the unsynchronized counter, while Python/Node/Go lose many — i.e. compiled languages are somehow safe here | **Wrong, and inverted.** At `-O0` the same C++ code loses ~1.9M updates. The zero was the optimizer hoisting the increment out of the loop into a single add — the race was compiled away, not won | **B** | The most instructive defect in Layer 1. It produced a *confident, memorable, and completely false* conclusion ("compiled languages don't lose updates") from a real program that really printed zero. Rule to keep: when a concurrency experiment reports a suspiciously clean result, rebuild at `-O0` before believing it, and treat any zero as a broken-experiment hypothesis first. |
| 2026-08-17 | `01-machine/05-blocking-vs-nonblocking-io`, `06-file-descriptors` | epoll behaviour and the `EMFILE` limit will be observable by running the provided code | **Never ran.** The C++ code `#include`s `sys/epoll.h` and the limit probe reads `/proc/self/limits`; neither exists on macOS. Both topics fail at build/run time on the only machine that was ever going to run them | **B** | Wrote Linux code for a macOS reader and never executed it once. Everything downstream in those two topics is unverified. Portable rewrite: `kqueue` alongside `epoll` with a `#ifdef`, or — better and consistent with the rest of the lab — run the probe *inside a Linux container*, which is where the production service lives anyway. |

<!-- Append new rows below this line. One per experiment. Under a minute. -->

## End-of-layer review notes

_Two sentences per layer: what did your misses have in common?_

- **Layer 1:** _(pending — Layer 1's own results are unverified; run the
  verification pass first, then write this.)_
