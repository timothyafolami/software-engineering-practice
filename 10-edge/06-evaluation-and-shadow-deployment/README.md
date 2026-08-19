# Layer 10 · Topic 6 — Evaluation design, and shadow deployment for models rather than code

### The takeaway (read this first)

**The one idea:** an eval score is a *measurement*, and measurements have
error bars and validity questions. Aggregate scores hide the failures that
matter, because the failure lives in a slice that is 2% of the set — and
when the grader is an LLM, the grader is an instrument that must be
validated against humans with a chance-corrected agreement statistic
before any number it produces means anything at all.

**Why it matters in practice:** most model-versus-model decisions are
made on a 2-4 point aggregate difference over about 200 items. For a
binary score near 50%, the standard error is `√(0.25/200) ≈ 3.5 points`,
so a 95% confidence interval is about `±1.96 × 3.5 ≈ ±7 points`. The
decision was noise — while the candidate regressed 15 points on the slice
that generates your support tickets, and the aggregate absorbed it.

**You'll know it landed when:** before running any comparison you write
down the decision rule, the slices, and the minimum effect size you would
act on — and you can state your judge's kappa against your own labels.

## The concept

### Anchored rubrics

A level defined by an adjective ("good, mostly accurate") is a Rorschach
test. A level defined by a concrete exemplar plus explicit
inclusion/exclusion criteria is an instrument. The test of a rubric is not
whether it sounds right when you read it — it is whether two humans
applying it independently agree.

### Agreement, chance-corrected

Raw percent agreement is meaningless with unbalanced labels: if 95% of
items are "fine", two raters who both always say "fine" agree 95% of the
time and have measured nothing. Chance-corrected statistics fix that:

```
κ = (p_observed − p_chance) / (1 − p_chance)
```

Cohen's kappa for two raters; Krippendorff's alpha when there are more
raters or missing labels. Working thresholds, which are conventions
rather than theory and should be quoted as such: below 0.4 means the
**rubric** is broken rather than the raters — rewrite it; 0.4-0.6 is weak
but fixable; above 0.6 acceptable; above 0.8 strong.

Measure **intra**-rater agreement too, by relabelling 30 items a week
later. If you do not agree with yourself, no judge can agree with you, and
your ceiling is set before any model is involved.

### Validating an LLM judge

Sample 100-300 *real production traces*. Have 2-3 humans label them on the
rubric. Compute human-human agreement first — that is your ceiling. Then
score the same items with the judge and report **judge-human kappa**
against that ceiling.

Design around the known pathologies:

- **Self-preference** — use a judge from a different model family than the
  system under test.
- **Position bias** in pairwise comparison — run both orderings and
  average; report the delta between them as a diagnostic in its own right.
- **Verbosity bias** — longer answers score higher for reasons unrelated
  to quality; check by regressing score on length.
- **Reliability is not validity**, and this is the one that matters most.
  A judge can be perfectly self-consistent and consistently wrong.
  Systematic evaluations published through 2026 make exactly this point;
  find them yourself rather than trusting an identifier printed in a
  README — a bare arXiv number for a recent paper is the classic shape of
  a confabulated citation, and this file deliberately does not hand you
  one.

### Statistics to attach

Bootstrap a confidence interval over *items*. When comparing two systems
on the same items, use a **paired** bootstrap: resample item indices once
and score both systems on that resample, so the shared per-item difficulty
cancels. The variance reduction is large and free, and an unpaired
bootstrap on paired data is the most common statistical error in eval
write-ups.

Then slice, and require **no slice regression** rather than aggregate
improvement. Keep an adversarial slice mined from real production
failures and refresh it every cycle; it is the only part of an eval set
that stays hard, because everything else gets fit to over time.

### Shadow deployment and rollback, for models

The code playbook does not transfer, because a model can be "up" and
wrong. There is no crash, no 5xx, no error rate to alert on.

- **Mirror** real traffic to the candidate, discarding responses or
  logging them for offline scoring.
- **Compare per-slice**, not in aggregate.
- **Gate promotion on a rule written before results exist**: no slice
  regression beyond the noise floor, latency SLO held, cost within budget.
- **Keep the previous model warm**, so rollback is a routing change in
  minutes rather than a redeploy.
- **Roll back the whole artifact set.** The weights are the smallest part
  of it: the prompt version, the rubric version and the judge version all
  roll back together, or you cannot reproduce the decision you made.

## How each language actually gets there

**Two, and the reason is that the mechanism here is statistical rather
than runtime-level:** a bootstrap in six languages is six identical
bootstraps. The one genuinely runtime-shaped question is *how do you
mirror traffic to a candidate without the shadow path being able to hurt
the live one*, and that has two interestingly different answers.

**Python** for the harness: eval set management, scoring, the paired
bootstrap, kappa and alpha, slice reporting. Shadowing in async Python is
`asyncio.create_task` plus a bounded semaphore and a hard timeout — and
the failure mode to know is that a task nobody awaits, which raises, will
surface its exception somewhere unrelated (or be swallowed at
interpreter shutdown), so the shadow path *can* affect the primary
through the back door of resource exhaustion or noisy logging.

**Go** is where traffic mirroring belongs when you want a shadow path
that structurally cannot affect the live one: the gateway duplicates the
request, fires the shadow call on its own goroutine with its own
`context.WithTimeout` and its own connection pool, and drops the result.
The separate pool is the load-bearing detail — sharing the primary's pool
means the shadow can exhaust it, which is how "shadowing is free" becomes
an incident.

## The experiment

1. **Build a 200-item eval set from real traffic**, with four named
   slices plus one adversarial slice mined from actual failures. Run
   [topic 5's](../05-pipelines-versioning-and-drift/README.md) MinHash
   contamination check on it *first* — an eval set overlapping training
   data measures memorisation.
2. **Label it twice.** You label 100 items; relabel 30 of them a week
   later (intra-rater); a second rater labels the same 100 (inter-rater).
   Compute Cohen's kappa for both. If it comes in under 0.4, rewrite the
   rubric and repeat — **that is the experiment succeeding**, not
   failing, and it is the step everyone skips.
3. **Build and validate the judge.** Score the same items with an LLM
   judge from a different family than the system under test. Report
   judge-human kappa alongside human-human kappa, plus the position-bias
   delta from swapping the order of pairwise comparisons.
4. **Run a real comparison.** Quantized versus unquantized from
   [topic 4](../04-quantization-and-determinism/README.md) is the obvious
   candidate and connects the layer. Per-slice scores with paired
   bootstrap 95% CIs. Write the promote/rollback rule down *before*
   looking at any result, and put it in
   [`PREDICTIONS.md`](../../PREDICTIONS.md) so you cannot revise it
   quietly.
5. **Shadow harness.** Mirror traffic to the candidate for a day through
   the Go gateway, score offline, and produce the promotion decision
   document — including the case for *not* promoting, written as if the
   result had gone the other way.

## How to run

**The statistics run with no server, no Docker and no GPU.** They are the
part you can validate today, and validating the instrument before pointing
it at a real decision is the whole argument of this topic. Each program
runs with no arguments on a synthetic case whose truth is known, and takes
files when you have them.

```
# agreement, chance-corrected -- and why raw percent agreement is worthless
python3 python/agreement.py
python3 python/agreement.py --a labels_me_r1.jsonl --b labels_rater2.jsonl

# is the judge scoring the answer or the slot it was shown in?
python3 python/judge_position_bias.py
python3 python/judge_position_bias.py --judgments both_orders.jsonl

# the comparison: paired bootstrap, per slice, against a rule fixed in advance
python3 python/compare.py
python3 python/compare.py --a scores_fp16.jsonl --b scores_q4.jsonl --bootstrap 10000

# the eval set as a sampling design rather than a folder of examples
python3 python/build_set.py
python3 python/build_set.py --from-traces traces.jsonl --n 200
```

Run the contamination check from topic 5 on the set **before** scoring
anything with it:

```
python3 ../05-pipelines-versioning-and-drift/python/minhash_contamination.py
```

`python/compare.py` with no arguments is the one to read first: it builds a
comparison where the aggregate shows no signal and one slice has fallen
off a cliff, which is the exact scenario the tool exists to catch. Check it
catches it before trusting it on a real decision.

Shadowing — the one genuinely runtime-shaped question in this topic, and it
runs locally too:

```
cd golang && go run shadow_gateway.go && cd ..
```

It mirrors identical shadow load two ways and measures what each does to
the **primary's** p99. The only difference between the rows is whether the
shadow draws from the same in-flight budget as the live path.

Then the real thing, against the stack. The candidate model server runs on
the **host** alongside the primary on a second port, for the Metal reason in
[`../lab/README.md`](../lab/README.md):

```
python3 -m mlx_lm.server --model ./q4  --port 8081     # primary,   on the host
python3 -m mlx_lm.server --model ./q8  --port 8082     # candidate, on the host

cd ../lab
SHADOW_TARGET=http://host.docker.internal:8082/v1 docker compose up -d gateway prom grafana
docker compose --profile load run --rm k6 run /scripts/arrival_rate.js -e RATE=2

curl -s localhost:8000/metrics | grep gateway_shadow_total
```

`SHADOW_TARGET` is read by the `gateway` service; unset it and shadowing is
off. `gateway_shadow_total{outcome=...}` is the only place the shadow's
failures are allowed to appear.

## Predict, then record

- Human-human kappa will be ___; intra-rater kappa ___.
- Judge-human kappa will be ___, i.e. ___ of the human-human ceiling.
- The aggregate difference between A and B will be ___ points, 95% CI
  ± ___.
- At least one slice will move opposite to the aggregate — I think it
  will be ___.
- Position-bias delta will be ___ points.

| Slice | n | model A | model B | paired Δ | 95% CI | regression? |
|---|---|---|---|---|---|---|
| | | | | | | |

| Agreement | value | n items |
|---|---|---|
| human-human (Cohen's κ) | | |
| intra-rater (Cohen's κ) | | |
| judge-human (Cohen's κ) | | |

**What would mean the experiment is broken rather than your prediction
wrong:**

- **Judge-human kappa above 0.9 on the first try.** Suspect the task is
  decidable from surface features — length, formatting, refusal keywords
  — or that the judge can see the reference answer. Read the
  disagreements; if there are only three, read all three.
- **Human-human kappa near 1.0.** Either the rubric is trivial or the
  raters discussed the items. Both make it useless as a ceiling.
- **A huge clean win on a slice with n=8.** Not a result. Set a minimum
  slice size before you start, and hold to it when the exciting number
  appears in the small slice.
- **Every CI excludes zero.** Check you used a *paired* bootstrap over
  items rather than an unpaired one, and that you are resampling items
  rather than resampling within a fixed score vector.
- **Shadow traffic changes primary latency.** The shadow is sharing a
  connection pool, a semaphore or a rate limiter with the primary. That
  is a real finding about your gateway, but it invalidates every latency
  number you took during the shadow window.

## Answer before moving on

1. Your judge has kappa 0.72 against you, and you have kappa 0.68 against
   yourself a week apart. What is the most you can honestly claim about
   the judge, and what would you have to do to raise the ceiling?
2. Derive the number of items you would need for a 95% CI of ±2 points on
   a binary score near 50%, and then say why that number is usually the
   wrong thing to spend the budget on.
3. A model improves 3 points aggregate and regresses 9 points on a slice
   that is 2% of traffic. Give the promotion decision and the reasoning,
   then give the *opposite* decision and the reasoning — and say which
   fact would settle it.
4. Shadow deployment catches quality regressions offline. Name two
   classes of failure it structurally cannot catch, and what you would
   pair it with for each.

## Next up

[Topic 7 — A transformer from scratch, and the economics of training
it](../07-transformer-from-scratch/README.md). Everything so far has
treated the model as a box with known costs. Topic 7 opens it, and prices
the training run before launching it.
