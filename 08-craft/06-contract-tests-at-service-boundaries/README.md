# Layer 8 · Topic 6 — Contract tests at service boundaries

### The takeaway (read this first)

**The one idea:** a contract test asserts that two independently-deployed
things still agree, *without deploying them together*. The hard part is not
the tooling — it is that the contract has to be an artifact separate from both
sides, or you have written a tautology.

**Why it matters in practice:** this is the failure that takes longest to
diagnose, because both services' test suites are green and the break exists
only in the composition. Nobody's CI is lying; nobody's CI was asked.

**You'll know it landed when:** you can explain why running schemathesis
against a FastAPI service's own generated OpenAPI schema proves *almost
nothing* on its own, and say exactly what you have to add to make it prove
something.

## The concept

Two families, and the 2026 answer is that you want both but start with one.

**Schema-first (provider-driven).** The provider publishes OpenAPI; tooling
generates requests from it and validates responses against it. Schemathesis is
the mature tool here — built on Hypothesis, supports OpenAPI 2.0/3.0/3.1 and
GraphQL, and has a stateful phase that chains operations (create → read →
delete) by following OpenAPI **links**. Cheapest possible onboarding: one CLI
command against a running service.

**Consumer-driven (Pact).** Each consumer records the subset of the API it
actually uses, as an artifact; the provider verifies against every consumer's
recorded expectations in *its own* CI. This catches the thing schemas cannot:
that you removed a field the spec always said was optional, but which one
consumer has depended on for two years. Worth adding once you have two or more
internal consumers whose expectations have drifted from the published spec —
which is most teams by year three. The framing that reconciles the two
families is **bi-directional**: the provider publishes its OpenAPI, consumers
publish their expectations, and a broker checks compatibility without either
side running the other's code.

**The trap, and it is specific to this stack.** FastAPI *generates* the
OpenAPI schema from your route signatures. The spec therefore cannot disagree
with the code — it is derived from it. Running schemathesis against
`/openapi.json` validates **internal consistency**: does the handler ever
500, does it return what its own annotations claim, does it honour the
parameters it declares. That is genuinely valuable and it is topic 3's audit.
But it is **not** a contract test, because a breaking change to your code
rewrites the contract at the same instant, and the tool then dutifully
verifies the new contract against the new code and reports success.

The fix is one line of discipline: **commit the schema.**
`openapi.snapshot.json` lives in the repository; CI regenerates the live one,
diffs it with a breaking-change differ, and fails the build on a breaking diff
unless the PR explicitly bumps a version. That converts a generated artifact
into an actual contract, and it costs one CI step.

A useful way to hold the whole topic: a contract test needs **three**
independent things — the provider's behaviour, the consumer's expectation, and
a written contract that outlives both. Any scheme with only two of the three
is checking that something equals itself.

## How each language actually gets there

**Three languages here, not six**, and the reason is the layer's own rule:
the mechanism lives *outside* the language. The contract is a JSON file on
disk; six clients generated from it would differ in syntax and in nothing
else. The three below are the ones with distinct *roles* — one provider, two
consumers who each learn about a break through a different mechanism — which
is the actual variable.

**Python (the provider).** Generate the schema in CI (`app.openapi()` dumped
to JSON), diff it against `openapi.snapshot.json`, and run schemathesis
against a live instance. FastAPI emits OpenAPI 3.1 by default on current
versions, which schemathesis handles. Two provider-side responsibilities that
are easy to skip: declaring the error responses (a 404 that is not in the
schema is a contract violation the tool cannot check), and declaring OpenAPI
**links** — without links the stateful phase has nothing to chain and will
report that it found none.

**Go (a consumer).** Generates a typed client from the committed snapshot
(`oapi-codegen` or similar). The break arrives at **compile time**: a field
that changed type stops compiling, and the failure names the field. This is
the cheapest contract enforcement in existence and it requires no broker at
all — but it only catches breaks that change the *shape* the generator saw.

**Node (a consumer).** Generates types from the same snapshot and tests
against a stub built from it. The break arrives at **typecheck or test time**,
and — importantly — a TypeScript client can be *structurally* satisfied by a
response that is semantically wrong, because types erase at runtime. The
contrast between how Go's build and Node's typecheck fail on the same diff is
the reason both consumers exist in this lab.

The pattern worth stealing if you are not ready for Pact: **the contract is a
committed file, and everybody generates from it.**

## The experiment

1. **Baseline audit.** Run schemathesis against the healthy service. All
   phases are enabled by default in v4 — `examples`, `coverage`, `fuzzing`,
   then `stateful`. Record findings by category (5xx, response-schema
   violations, missing `Content-Type`, ignored parameters).
2. **Stateful, deliberately.** Re-run with `--phases=stateful` to isolate the
   chained-operation phase, and record whether it finds anything the stateless
   phases did not — specifically, whether `DELETE` then `GET` behaves as the
   spec claims. If it reports **`Missing Open API links`**, that is a finding
   about your schema, not a tool failure: your spec never said which
   operations can follow which, so no tool can chain them. Add the links and
   re-run.
3. **Make a breaking change and see who notices.** Change `total: int` to
   `total: str` in the response model. Now run, in order: (a) the provider's
   own unit tests, (b) schemathesis against the *live* schema, (c) the
   snapshot diff, (d) `consumer-go`'s build, (e) `consumer-node`'s tests.
   Record which of the five caught it. The interesting result is which ones
   stay green.
4. **Repeat with a subtler break:** make an existing required response field
   optional. This is *not* breaking for the provider and *is* breaking for
   consumers, which is the entire argument for consumer-driven contracts,
   compressed into one diff.

## How to run

```
cd 08-craft/lab && docker compose up -d api postgres

# v4 CLI: all phases run by default; the schema URL is positional
docker compose --profile tools run --rm tools schemathesis run http://api:8000/openapi.json
docker compose --profile tools run --rm tools schemathesis run http://api:8000/openapi.json --phases=stateful
docker compose --profile tools run --rm tools schemathesis run http://api:8000/openapi.json --report junit

# the snapshot diff -- the only line in this block that is a contract test
curl -s http://localhost:8010/openapi.json > /tmp/live.json
docker compose --profile tools run --rm tools oasdiff breaking api/openapi.snapshot.json /tmp/live.json
```

Three of the five checks run natively, with no daemon:

```
cd 08-craft/lab/api            && python3 snapshot_openapi.py --check   # the CI gate
cd 08-craft/lab/api            && DATABASE_URL=sqlite+aiosqlite:///:memory: pytest tests/contract -q
cd 08-craft/lab/consumer-go    && go build ./... && go test ./...
cd 08-craft/lab/consumer-node  && node --test
```

`snapshot_openapi.py` writes `openapi.snapshot.json` with no arguments and, with
`--check`, prints a unified diff and exits 1 on any drift -- the thirty-second
version of the gate you can add today. `tests/contract/` additionally asserts
that every path-parameterised GET declares a 404, that `POST /orders` declares
the OpenAPI **links** the stateful phase needs (`GetOrderById`, `DeleteOrderById`,
both using `$response.body#/id`), and that `DELETE /orders/{id}` still has no 404.

Both consumers test against a **stub built from the committed snapshot**, never
against the live API -- running them against the live service is an integration
test and will not survive the two being deployed independently.

A finding fell out of building this, and it belongs in the record rather than in
a footnote: **FastAPI's generated schema was not byte-stable across processes.**
Two modules both declared a model called `OrderOut`, FastAPI disambiguated by
prefixing the module path, and *which* of the two got prefixed varied per run --
so `openapi.snapshot.json` diffed against itself. A snapshot that cannot be
reproduced is not a contract. Every response model in the lab now has a globally
unique class name, and `snapshot_openapi.py --check` passes repeatedly. This is
topic 9's naming argument arriving as a topic 6 defect.

**Blocked on this machine, with the exact unblock command:**

| What | Why | Unblock |
|---|---|---|
| `schemathesis`, `oasdiff` | run inside the `tools` image; the Docker daemon is not running | start Docker Desktop, or `python3 -m pip install 'schemathesis==4.24.*'` for the host |

The four native commands above all pass here: 4 contract tests, 4 Go tests, 7
Node tests.

**Flag notes for schemathesis v4**, because the v3 spellings are still all over
the internet and several of them fail silently rather than loudly: stateful
testing is a **phase**, selected with `--phases=examples,coverage,fuzzing,stateful`
(there is no `--experimental=` flag for it in v4). All checks run by default, so
`--checks` is for *narrowing* and `--exclude-checks` for skipping one. `-b` /
`--base-url` became `-u` / `--url`, `--junit-xml` became `--report junit`,
`--exitfirst` became `--max-failures=1`, and `--hypothesis-seed` became
`--generation-seed`.
## Predict, then record

Predict which of the five checks catch each break, before running any of them.
Fill in every cell with your prediction first, in pencil, then overwrite with
what happened — the disagreements are the finding.

| Break | provider unit tests | schemathesis (live) | snapshot diff | consumer-go build | consumer-node tests |
|---|---|---|---|---|---|
| `total: int` → `str` | | | | | |
| required response field → optional | | | | | |
| new optional field added | | | | | |
| error response removed from schema | | | | | |

| Phase | findings | categories |
|---|---|---|
| default (all phases) | | |
| `--phases=stateful` only | | |

**What would mean the experiment is broken rather than your prediction wrong:**

- **schemathesis catches the `int → str` change.** Almost certainly you
  pointed it at the *committed snapshot* rather than the live schema. Check
  the URL. Pointed at the live `/openapi.json` after the change, the spec
  already agrees with the code and it should report nothing — **that null
  result is the finding**, and it is the whole point of the topic.
- **The stateful phase reports `Missing Open API links`.** Not a broken tool.
  Your schema declares no links, so there is nothing to chain. Add links to
  the spec, or accept that this phase cannot run and say so in the record.
- **The snapshot diff reports a breaking change for the "new optional field"
  row.** Your differ is configured to treat any addition as breaking, which
  makes the gate useless inside a week. Tune it before you ship it, and record
  what you tuned.
- **`consumer-node` passes the `int → str` break.** Check whether it actually
  validates the response at runtime or only at typecheck. If the client
  `JSON.parse`s into a typed variable and never asserts, TypeScript will
  believe you, and this is a real and common finding rather than a broken
  experiment — but write down which one it is.
- **Every check catches everything.** You are probably running the consumers
  against the live service rather than against a stub built from the snapshot.
  That is an integration test, not a contract test, and it will not survive
  the two services being deployed independently.

## Answer before moving on

1. Adding an optional response field is non-breaking under every schema
   differ. Construct a real consumer for which it *is* breaking.
2. Pact requires the provider to run every consumer's expectations in its CI.
   What organisational problem does that create, and how would you mitigate it
   without abandoning consumer-driven contracts?
3. Your Go and Node consumers generate from the committed snapshot. Name a
   failure that scheme completely misses and a Pact-style setup would catch.
4. Schemathesis against a generated schema proves internal consistency. Write
   the one-sentence version of what that *is* worth, phrased so a skeptical
   colleague would accept it.

## Next up

[Topic 7 — Fault injection: make the dependency slow, not absent](../07-fault-injection-slow-not-absent/README.md):
the only experiment in this layer that reproduces the shape of a real
production incident, where nothing is down and everything is late.
