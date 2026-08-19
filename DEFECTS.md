# Known defects in the drafted layers

Found by an adversarial review agent after the breadth pass, 2026-08-18.
Nothing here is fixed in the layer files yet — the code pass is writing to
those same files right now, so applying these mid-run would collide. Apply
after it finishes.

**Verified** = I re-checked it by hand and it is real.
**Reported** = the review agent claims it; plausible, not independently confirmed.

## Fixed already (root files only)

| File | Defect | Fix applied |
|---|---|---|
| `README.md` | Declared a three-language policy and called six "a mistake" | Rewritten to six. That section was written on my wrong assumption, not yours |
| `SEQUENCE.md:114` | Instructed "Drop Rust/C++/Java while you are in there" | Reversed — the C++/Rust defects are bugs to fix, not a reason to drop the languages that exposed them |
| `README.md:137`, `PREDICTIONS.md:5` | "72 topics"; the table sums to 79, and the derived "seventeen months" followed from it | 79 topics, eighteen months. **Verified** by summing the table |

## Pending — apply after the code pass

### Wrong facts

| File | Defect | Status |
|---|---|---|
| `01-machine/07/README.md:251` | `cpu.max = 100000 50000` described as "same 1.0 CPU". Format is `QUOTA PERIOD`, so that is **2.0 CPUs**. Two rows of the headline table become the same quota and the reader concludes period length caused the fix | **Verified** — should be `50000 50000` |
| `07-security/README.md:42` | "A01 for the fourth edition running". Broken Access Control has been A01 since 2021 — it was A5 in 2017. Two editions, not four | **Verified** — delete the clause |
| `05-failure/README.md:13` | "0.7x more latency going 50%→60%". The `1/(1−ρ)` formula in the same sentence gives 2.0 → 2.5, i.e. **0.5x**. A number contradicting the model stated one clause earlier, in the layer's opening argument | **Verified** |
| `10-edge/README.md:98` | "~50 for an M1 Pro (≈10 TFLOP/s over 200 GB/s)". 200 GB/s is M1 Pro; ~10 TFLOP/s is M1 **Max**. M1 Pro is ≈5.2 TFLOP/s → ridge point ≈26 | Reported |
| `08-craft/README.md:970` | `schemathesis run --experimental=stateful` — wrong in both v3 (the value was `stateful-test-runner`) and v4 (replaced by phases) | Reported |

### The no-fabrication rule leaked into prose

The rule held for tables — no results table anywhere ships pre-filled, which
was the Layer 1 failure and it is genuinely fixed. But specific numbers got
asserted as fact in body text instead:

- `07-security/README.md:794-800` — four precise supply-chain statistics
  (~29 million hardcoded secrets, a 34% jump, ~94 days median rotation, 64%
  still valid) with **no citation anywhere in the file**. This is the exact
  shape of the Layer 1 table: plausible, memorable, unverifiable in place.
- `05-failure/README.md:338` — vendor performance figures stated flat rather
  than as "their published numbers".
- `09-writing:71`, `10-edge:1035` — bare arXiv IDs with no titles. A bare
  post-cutoff number is the classic shape of a confabulated citation.

**Rule to add to `README.md`:** extend "no pre-filled tables" to *every number
in prose is either derived on the page or carries a source.* That single edit
catches all of these.

### Experiment does not test its claim — the Layer 1 defect, recurring

`08-craft/README.md:768-802`, the layer's flagship. `page()` documents the
precondition "rows sorted by `created_at` DESC", but the Hypothesis strategy
generates lists in arbitrary order. It will fail on a two-row *unsorted* list
long before it ever produces the tie the topic is about — so the reader gets a
confident false confirmation, and the layer's own "you own this when" test
(watch Hypothesis produce a two-element counterexample) is not built by its
experiment.

Fix: `.map(lambda rs: sorted(rs, key=lambda r: r.created_at, reverse=True))`,
and add to the broken-experiment checklist: *"counterexample has no repeated
`created_at` → you found a precondition violation, not the tie bug."*

### Coverage gaps against the roadmap

- **The storage engine project has no home at all.** Roadmap project #3 (WAL,
  B-tree or LSM, crash recovery, kill it mid-write) appears in zero files and
  is not in `SEQUENCE.md`'s deliberate-skip list either. Largest single hole.
- **OAuth2 / OIDC flows** — a named Layer 7 bullet. Topic 4 covers JWT
  structure and revocation; authorization-code + PKCE gets no topic.
- **Naming** — a named Layer 8 design bullet, zero occurrences in `08-craft/`.
- **Layer 1's four original missing experiments** (allocation cost, memory
  visibility, deadlocks, context switches) are still unscheduled. `SEQUENCE.md`
  Block B fixes the four *defects*, which are a different list.
- **`Database Internals` (Petrov)** — a roadmap Layer 3 resource, absent from
  Layer 3's otherwise careful resource-update section.

### Consistency

- Version pins drift despite `SEQUENCE.md:378` promising they are set once:
  k6 v1 vs v2, Postgres 17 vs 18.6, Python 3.13 vs 3.14 across files.
- `README.md:127` claims Layer 8 is the only roadmap layer with no "You own
  this when" block. Layer 10 has none either, and `10-edge` silently invents
  one — the thing Layer 8's note congratulates itself for not doing.
- `01-machine/README.md` still describes the six-language rationale. That is
  now **correct** and should be left alone; it was the root README that was
  wrong.

### Weakest layer: 07-security

The only layer that does not meet the lab's own bar. No `lab/` spec (its run
blocks invoke `./attack.sh` and `seed.py`, never defined), nothing extends
`lab-harness/`, and its record tables are qualitative yes/no cells — so most
of Layer 7 cannot produce a row the prediction log will accept. It also
carries the one checkably-false claim and the largest uncited statistics
block, and `SEQUENCE.md` schedules it last, so it will be the least-corrected
material in the repo at the moment it matters most.
