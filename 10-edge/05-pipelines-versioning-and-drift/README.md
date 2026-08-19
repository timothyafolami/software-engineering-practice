# Layer 10 · Topic 5 — Data pipelines, versioning, and drift: where ML systems actually fail

### The takeaway (read this first)

**The one idea:** the model artifact is the least interesting version in
the system. A prediction is reproducible only if you can name the triple
*(code version, data version, feature-transform version)* — and the most
common production ML failure is still **training/serving skew**: features
computed one way offline and subtly differently online.

**Why it matters in practice:** skew does not page you. Quality degrades
while every dashboard stays green, because the serving path *is* working
— just on different numbers than the model was fit on. A window
definition ("7-day average" over complete days online, over all available
history offline), a schema change upstream, a null that became a zero, a
timezone. The fix that holds is structural, not vigilant: one
implementation of the transform used by both paths, plus a contract test
that recomputes online features offline and asserts equality.

**You'll know it landed when:** you can look at a feature definition and
say whether it is point-in-time correct, and write the test that fails if
someone changes the online path.

## The concept

### Point-in-time correctness

A training row's features must contain only information that existed at
the moment the prediction would have been made. Violate it — join a
label-day aggregate into a row whose label comes from that same day — and
you get beautiful offline metrics and a useless model.

This is the ML restatement of a read anomaly, and your
[`03-data`](../../03-data/README.md) isolation-levels work is the right
mental model: both are "which version of the world was visible to this
computation, and did you actually pin it." The offline query needs an
`as_of` timestamp threaded through every join, in the same way a
repeatable-read transaction needs a snapshot.

### Versioning, and what changed

MLflow remains the default experiment tracker. The data-versioning story
shifted: **lakeFS acquired DVC (announced November 2025)**, so the old
"DVC vs lakeFS" framing is obsolete — verify against the acquiring
company's own announcement before repeating it, since this is exactly the
kind of fact that gets garbled in secondary sources. Meanwhile, for
tabular data, table formats (Iceberg, Delta) increasingly do the
versioning job with time travel built in, leaving DVC's sweet spot as
file-blob datasets inside a git-centric repo.

The requirement underneath is unchanged and outlives the tooling: every
production prediction traceable to a (code, data, transform) triple.

### Drift metrics are information theory

Knowing that is what makes you use them correctly rather than
superstitiously.

**KL divergence** `D(P‖Q) = Σ p log(p/q)` is the expected number of extra
bits needed to code samples from P using a code built for Q — literally
"how surprising is today's traffic under the training distribution." It
is asymmetric and unbounded, and it is undefined where `q = 0`, which is
why every implementation smooths.

**PSI** is a binned, symmetrised cousin:

```
PSI = Σ_bins (p_i − q_i) · ln(p_i / q_i)
```

Because it is the symmetrised sum, PSI is bounded-ish in practice and has
conventional thresholds — >0.1 investigate, >0.2 act — which are **rules
of thumb from credit-risk practice, not theory**. Bin count changes the
value materially; fixing bin edges once, from the training window, and
never re-deriving them is the difference between a monitor and a random
number generator.

The operational point that separates people who have run this from people
who have read about it: **drift without a measurable quality drop is a
false alarm, and false alarms burn on-call**. Alert on the joint
condition — distribution shift *and* an eval decline — and keep the
false-alarm rate as a number you can quote.

### Sketches, from the same toolbox

Each trades a *stated* error for orders of magnitude of memory. Know the
derivation so you can answer "how wrong could this be" when asked:

- **HyperLogLog** for cardinality. Standard error ≈ `1.04/√m` for `m`
  registers. At `m = 16,384` registers of 6 bits — 12 KB — that is
  `1.04/128 ≈ 0.8%`, for a set of any size. Halve the memory to `m=4096`
  (3 KB) and the error roughly doubles to ≈1.6%.
- **MinHash / LSH** for near-duplicate detection between training and
  eval sets, which is how you catch contamination without an all-pairs
  comparison. Jaccard estimate error falls as `1/√k` in the number of
  hash functions.
- **Count-Min** for heavy hitters, with a *one-sided* bound: it never
  underestimates, and overestimates by at most `εN` with probability
  `1 − δ` for a table of width `e/ε` and depth `ln(1/δ)`.

## How each language actually gets there

**Three, and the reason is the mechanism itself:** training/serving skew
is *born* at a language boundary. It takes two independent
implementations of one transform to create it, so this topic needs
exactly two runtimes to reproduce it honestly, and a third to show how
fast it compounds. Rust, C++ and Java are omitted here because a fourth
copy of the same transform would demonstrate nothing the second and third
have not — and "we reimplemented it once more" is precisely the anti-fix
this topic is arguing against.

**Python** is the offline path and the serving path: SQL for the offline
aggregates, the *same* transform module imported by the FastAPI handler,
`pytest` for the contract test, `datasketch` for MinHash, `polars` or
`pandas` for drift computation.

**Go** is the ingest/CDC side, and the relevant fact is that a Go ingest
service carrying its own copy of the transform logic is exactly how skew
is born — not through incompetence, but because the Python module was not
callable from there and reimplementing forty lines looked cheaper than
standing up an RPC. Rounding differs (`math.Round` is half-away-from-zero;
Python's `round` is banker's rounding), null handling differs, and time
zone handling differs. Reproduce that gap deliberately, then fix it the
only way that holds: the transform gets **one home**, and everything else
calls it across a boundary.

**Node.js** appears as the third implementation, in the shape it usually
arrives in production: a small feature computed in the frontend BFF
"because it was easier there." Its float64-only arithmetic and its
timezone-by-default behaviour give a third set of small differences,
which is the point — three implementations produce three answers, and
nobody notices because each is individually reasonable.

## The experiment

1. **Build the two paths.** Postgres from
   [`../lab/README.md`](../lab/README.md) with `events (user_id, ts,
   amount)` and `labels`. Offline: SQL producing a 7-day rolling
   aggregate per user *as of* a prediction timestamp. Online: the FastAPI
   handler computing the same feature at request time.
2. **Introduce the classic skew deliberately.** Offline includes the
   partial current day; online uses only complete days, because that is
   what the cached aggregate holds. Train a small model, measure offline
   AUC, replay the same rows through the online path, measure again, and
   record the gap. Then add the Go and Node reimplementations and record
   how many of the 500 rows disagree across *all three*.
3. **Write the contract test that catches it.** Sample 500 recent
   production feature vectors, recompute them from the offline path at
   the logged prediction timestamps, assert equality within a stated
   tolerance, and report the top offenders by absolute difference. **This
   test is the deliverable** — a genuine lift-to-work artifact you can
   paste into a real repository on Monday.
4. **Drift, done properly.** Per-feature PSI and KL between the training
   window and rolling live windows; *separately*, actual model quality on
   labelled live data; then correlate the two. Count how many PSI > 0.2
   alerts had no quality impact — that number is your false-alarm rate and
   the entire argument for joint alerting.
5. **Contamination check.** MinHash the eval set against the training
   data at a stated Jaccard threshold and report the near-duplicate count.
   Do this *before* you trust any number in topic 6.

## How to run

**Nothing here needs the GPU, the model server, or Postgres.** The topic is
about two implementations of one transform disagreeing, and the cheapest
honest way to show that is one deterministic event log that three languages
read byte-for-byte. `../lab/`'s `db` and `api` are the production-shaped
version of the same thing and the SQL in `../lab/db/init.sql` matches; use
them when you want the Postgres path, not to see the result.

```
pip install numpy

# one deterministic event log, so a disagreement can never be the input
python3 python/seed_events.py

# the transform, and its self-check on the three boundaries the spec pins
python3 python/features.py

# two reimplementations, each written the way it really happens, each also
# able to conform to the written spec
cd golang && go run features.go && \
  go run features.go -mode conform -out ../data/features_go_conform.csv && cd ..
node nodejs/features.js && node nodejs/features.js --mode conform

# three implementations, three answers -- with each disagreement attributed
# to one decision rather than to "a float thing"
python3 python/three_way_diff.py
```

Then the deliverable, which is the point of the topic:

```
python3 python/test_feature_contract.py                 # native log: FAILS, names the rows
python3 python/test_feature_contract.py --log conform   # PASSES
pytest python/test_feature_contract.py                  # if you would rather wire it into CI
```

Run both. A guard that has never failed has not been shown to work.

The rest of the experiment:

```
python3 python/offline_online_skew.py       # partial current day -> AUC gap
python3 python/drift_psi_kl.py              # PSI/KL, joint alerting, false-alarm rate
python3 python/minhash_contamination.py     # run this BEFORE trusting topic 6
```

`python/drift_psi_kl.py` is the one to read closely: the scenario that trips
the PSI > 0.2 threshold costs no accuracy at all, and the scenario that
costs 0.09 AUC does not move PSI. That pair is the argument for alerting on
the conjunction.

The Postgres path, if you want the production shape rather than the result:

```
cd ../lab
docker compose up -d db api
docker compose exec db psql -U app -c "select count(*) from items;"
```

`data/` is generated and git-ignored; `python3 python/seed_events.py`
rebuilds it identically from the seed.

## Predict, then record

- Offline AUC will be ___; online AUC on the same rows will be ___.
- PSI on the deliberately skewed feature will be ___.
- Of my PSI > 0.2 alerts, ___% will have no measurable quality impact.
- The Python/Go/Node three-way diff will disagree on ___ of 500 rows,
  and the largest single cause will be ___.
- My eval set will be ___% near-duplicate against training data.

| Feature | offline | online | mismatch rate | PSI | KL | eval impact? |
|---|---|---|---|---|---|---|
| | | | | | | |

| Implementation pair | rows differing / 500 | max abs diff | cause |
|---|---|---|---|
| Python vs Go | | | |
| Python vs Node | | | |
| Go vs Node | | | |

**What would mean the experiment is broken rather than your prediction
wrong:**

- **Offline and online agree exactly.** The "online" path is almost
  certainly reading the offline feature table. Verify by changing the
  offline SQL and confirming the online value does *not* move.
- **PSI is 0.0 across every feature.** You are comparing a window to
  itself. Check the window boundaries, and check both sides were not
  computed from one snapshot.
- **PSI is enormous on every feature.** Look for a units or encoding
  change — cents versus dollars, a re-encoded categorical, a changed bin
  edge — before concluding the world drifted. Real drift is rarely
  uniform across unrelated features.
- **Zero near-duplicates at threshold 0.8.** Check the MinHash is
  actually being fed normalised text; whitespace and casing differences
  make identical documents look distinct, which produces a comforting and
  meaningless zero.
- **The contract test passes on the first run.** Either you wrote the
  tolerance too wide, or you are comparing the offline path to itself.
  A test that has never failed has never been tested; break the online
  path on purpose and confirm it goes red.

## Answer before moving on

1. Give a feature definition that is *not* point-in-time correct but
   passes every unit test, and explain how it would look in the offline
   metrics.
2. PSI thresholds of 0.1 and 0.2 are conventions with no theory behind
   them. Derive what PSI value a *harmless* change would produce for a
   feature whose bin edges were re-derived from the live window, and
   explain why that alone can trip your alert.
3. You have one transform in Python and one in Go and cannot merge them
   this quarter. Name three mitigations ranked by how much they actually
   reduce risk, and say what each costs.
4. HyperLogLog gives ≈0.8% error in 12 KB. Your product manager asks for
   exact distinct counts. Compute what exact counting would cost for
   100M distinct user ids, and state the one situation where you should
   pay it.

## Next up

[Topic 6 — Evaluation design, and shadow deployment for models rather
than code](../06-evaluation-and-shadow-deployment/README.md). You now have
a drift signal that fires. Topic 6 is how to tell whether the thing it
fired about actually got worse — which turns out to be a measurement
problem with error bars, not a scoring problem.
