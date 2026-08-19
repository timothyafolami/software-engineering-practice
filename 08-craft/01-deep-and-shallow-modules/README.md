# Layer 8 · Topic 1 — Deep modules, shallow modules, and change amplification

### The takeaway (read this first)

**The one idea:** a module's value is the ratio between how much it *does* and
how much you must know to use it. A module that adds an interface without
absorbing complexity has negative value — it makes the system bigger and no
part of it simpler, while looking exactly like good architecture in a diagram.

**Why it matters in practice:** the four-layer `router → service → repository
→ dao` shape is the default in Python service codebases, and in most of them
three of those four layers forward calls. It is the most common way a codebase
becomes expensive without anyone ever making an obviously bad decision.

**You'll know it landed when:** you can look at a proposed abstraction and
predict its *change amplification* — how many files a typical future
requirement will touch — before it is written, and you use that number instead
of an opinion when you argue about it in review.

## The concept

Ousterhout's framing, and the reason it is worth adopting, is that complexity
has exactly three symptoms and all three are observable rather than aesthetic:

- **Change amplification** — a simple change requires edits in many places.
  Countable with `git diff --stat`.
- **Cognitive load** — how much a developer must know to complete a task.
  Countable as the number of files you must have open at once to answer a
  specific question about behaviour.
- **Unknown unknowns** — it is not obvious which code must change. The worst
  of the three, because you cannot even bound the risk, and the only one that
  resists direct measurement.

**Depth** is `functionality provided ÷ interface surface`. `open()` is deep:
three arguments, and behind them sit buffering, permission checks, descriptor
allocation, path resolution, and a filesystem. A `UserRepository.get_by_id(id)`
whose body is one line calling `session.get(User, id)` is shallow: the
interface is as complicated as the implementation, so every caller pays the
cost of learning it and gets nothing back.

The tell for a shallow module, and it is reliable: **reading its
implementation teaches you nothing you had not already inferred from its
name.** If the body holds no surprise, the body was not worth hiding.

The non-obvious corollary is that **layers are not free**. A pass-through
method — one whose signature is nearly identical to the one it calls — is
strictly worse than no method, because now there are two places to look, two
names for one idea, and two things to keep in sync. The fix for a shallow
module is usually to *delete* it and let the caller talk to what it wrapped.

Where this gets genuinely hard is that the *right* deep module is not the one
with the biggest body. Depth is about the interface being small relative to
what is behind it. A 400-line function with eleven parameters is not deep, it
is just large. The two failure directions have different smells and the same
cause: nobody measured.

## How each language actually gets there

Six languages. The mechanism under study is what each language's *toolchain
makes cheap* — because a shallow layer appears wherever adding one costs the
author nothing, and disappears wherever the compiler charges for it. That is a
real property of six different toolchains, not a restatement of one idea.

**Python (your stack).** Nothing charges for a layer. No compile step, no
build-graph edge, no signature to repeat — and FastAPI's `Depends()` makes
wiring a new layer a one-line decorator. The forwarding layer is nearly always
the "repository": it exists because a 2015-era blog post said to decouple from
the ORM, but SQLAlchemy 2.0's `Session` *is already* that abstraction. A
repository on top of it has two options and both are bad: re-expose
SQLAlchemy's full expressiveness (a pass-through) or restrict it (and then
every non-trivial query needs an escape hatch, so `repo.session` leaks anyway
and you have a pass-through with extra steps). The honest deep version owns a
*use case* end to end — `place_order(...)` — including its transaction
boundary.

**Node.** The same failure mode, plus two Node-specific amplifiers. Layers
that are `async` all the way down add a microtask hop each, so the indirection
has a small but real runtime cost stacked on the cognitive one. And in
TypeScript the DTO is usually redeclared per layer, which makes change
amplification *literally* the count of `.d.ts`-shaped edits — a one-field
requirement touches an interface per layer before it touches any logic.

**Go.** The language actively resists shallow layers: no default arguments, no
decorators, and interfaces declared at the *consumer* rather than the
producer, so a wrapper has to be written out in full and justify itself with
real code. `internal/` gives you a compiler-enforced statement of "this is not
interface surface", which no other language here has as a first-class package
concept. Go codebases fail the *other* direction — under-abstraction, with
error wrapping and validation duplicated at every level.

**Rust.** The type system makes shallowness visible in the signature itself.
A wrapper that does not absorb anything has to repeat the callee's lifetimes,
generic parameters and error type, so the pass-through is character-for-
character almost identical to what it wraps — you can *see* the ratio rather
than reason about it. Visibility is compiler-enforced (`pub`, `pub(crate)`,
`pub(super)`), and `cargo public-api` will print the exact public surface as a
list, which turns "interface surface" from a judgement call into a diffable
artifact. This is the one language here where a CI job can fail a PR for
*widening the interface* without anyone reading it.

**C++.** The cost of a shallow layer is paid in the build, and it is the only
language in this lab where you can watch that happen. A header included widely
creates a physical dependency: touch it, and every translation unit including
it rebuilds. That is why the pimpl idiom exists at all — it buys an interface
that does not leak implementation into every consumer's compile. Two numbers
your toolchain will print for you: preprocessed translation-unit size
(`g++ -E file.cpp | wc -l`) and rebuild time after touching one header. Both
are proxies for interface surface that no amount of arguing can talk down.

**Java.** The canonical shallow layer has a name: `OrderService` +
`OrderServiceImpl`, an interface with exactly one implementor, existing
because a framework or a mocking library wanted a seam. Spring's DI makes
adding one free, exactly like FastAPI's `Depends()`. The mechanical audit is
easy and worth running on any real codebase: count interfaces with a single
implementor. Each is a candidate pass-through, and the honest ones are the
few that exist because a *second* implementation is genuinely planned or
because the seam is a test double you decided to keep.

## The experiment

`app/shallow/` and `app/deep/` implement the *identical* feature — `GET
/customers/{id}/orders` with filtering and a total count — one as four
forwarding layers, one as a single substantial module with a genuinely small
interface. Both are mounted in the same FastAPI app on different route
prefixes, and both pass the same integration tests. That last part matters:
if the two shapes did not pass the same tests, any difference you measure
afterwards is a difference in behaviour, not in design.

Then apply one realistic requirement to each and measure:

> "Orders can be filtered by `status`, and the `total` in the response must
> reflect the filter, not the unfiltered count."

Record, mechanically, for each shape:

1. **Change amplification** — `git diff --stat` after implementing it: files
   touched, lines changed.
2. **Cognitive load** — the number of distinct files you must have open to
   answer "what happens if `customer_id` doesn't exist?" Count honestly,
   including the ones you opened and closed again.
3. **Interface surface** — the public names each package exports, plus total
   parameters across all public functions.
4. **Depth ratio** — public signature + docstring lines versus implementation
   lines.

**Two cross-language probes, and why only two.** The change-amplification half
needs a realistic service, so it runs in Python only — porting a four-layer
FastAPI app to six languages would measure your patience. But interface
surface is a number two toolchains will simply *print*, so this topic borrows
them: build the same two shapes as a ~100-line library in Rust and in C++, and
read the surface off `cargo public-api` and off rebuild time after touching a
header. The point is not the languages; it is seeing the same property you
counted by hand in Python come out of a tool that cannot be argued with.

## How to run

The Python half needs the stack; everything under `probes/` is native macOS.

The precondition first -- and it is a test, not an assumption. Both shapes must
return byte-identical bodies before any number measured against them means
anything, so that is asserted rather than asserted-about:

```
cd 08-craft/lab/api && DATABASE_URL=sqlite+aiosqlite:///:memory: \
  python3 -m pytest tests/integration/test_shapes_are_identical.py -q
```

Seven tests, native, no daemon: five parameterised page/offset comparisons of
the raw response BYTES, the 404 both ways, and requirement 0's unfiltered
`total` pinned so a later diff is against a state somebody checked. The same
file runs against Postgres 18 inside the stack by pointing `DATABASE_URL` at it.

```
cd 08-craft/lab && docker compose up -d api postgres
docker compose exec api make seed
docker compose exec api pytest tests/integration -q          # incl. the shape check above

# interface surface, counted rather than argued about
docker compose exec api python -c \
  "import app.shallow as m; print(len(m.__all__), sorted(m.__all__))"
docker compose exec api python -c \
  "import app.deep as m; print(len(m.__all__), sorted(m.__all__))"

# change amplification: implement the status filter in BOTH shapes, then
git switch -c t1-requirement
git diff --stat -- 08-craft/lab/api/app/shallow 08-craft/lab/api/app/deep
```

The two shapes are `lab/api/app/shallow/` (`router.py` -> `service.py` ->
`repository.py` -> `dao.py`, a DTO declared per layer) and `lab/api/app/deep/`
(`orders.py`, one public function plus the type it returns, private helpers
behind it). Both are mounted in the same app, on `/shallow` and `/deep`.

The two probes run natively, no container:

```
cd 08-craft/lab/probes/rust && cargo test          # both shapes behave identically
cargo public-api -p shallow                        # count the entries
cargo public-api -p deep                           # count the entries

cd 08-craft/lab/probes/cpp && ./measure.sh         # TU lines + rebuild seconds, both shapes
```

`measure.sh` prints preprocessed translation-unit size and best-of-three rebuild
time after touching one header, for both shapes, and runs both binaries so you
can confirm they agree before believing any number either produced.

**Blocked on this machine, with the exact unblock command:**

| What | Why | Unblock |
|---|---|---|
| everything under `docker compose` | the Docker daemon is not running (`docker info` fails) | start Docker Desktop |
| `cargo public-api` | not installed | `cargo install cargo-public-api --locked` |

`cargo test`, `./measure.sh` and `tests/integration/test_shapes_are_identical.py`
all run here as-is.
## Predict, then record

Before you touch anything, write down: how many files does the status filter
touch in the shallow shape? In the deep shape? And — the interesting one —
which shape do you expect to have *more total lines of code* after the change?

| Metric | shallow | deep |
|---|---|---|
| files touched | | |
| lines changed | | |
| files open to answer the 404 question | | |
| public names | | |
| total public params | | |
| depth ratio (interface lines : body lines) | | |

| Probe | shallow | deep |
|---|---|---|
| Rust: public API entries (`cargo public-api`) | | |
| C++: preprocessed TU lines | | |
| C++: rebuild seconds after touching one header | | |

**What would mean the experiment is broken rather than your prediction wrong:**

- **The shallow shape touches only one file.** Then you did not build a
  realistically shallow version. A real four-layer codebase declares the DTO
  once per layer; if yours passes the same object all the way down, you have
  already built the deep version with extra function calls in it.
- **The deep shape is a 200-line function with no internal structure.** You
  built a *big* module, not a *deep* one. Depth is about the interface being
  small, not the body being large, and a body with no seams inside it has
  traded one problem for another. Rebuild it with private helpers and
  re-measure the public surface only.
- **Both shapes touch the same number of files.** Check whether your "layers"
  actually enforce anything. Four files that all import each other freely are
  one module in four pieces, and this experiment cannot distinguish them.
- **The integration tests fail on one shape.** Stop and fix that first. Any
  measurement taken while the two shapes behave differently is measuring the
  difference in behaviour, and you will attribute it to design.

## Answer before moving on

1. Ousterhout says pass-through methods are worse than no method. Name a case
   where a pass-through is genuinely correct anyway — and say what makes it
   different from the ones in `app/shallow/`.
2. Your `deep` module owns its transaction boundary. What breaks the first
   time a caller needs to compose *two* deep modules inside one transaction,
   and what does that tell you about where transaction boundaries actually
   belong?
3. "Interface surface" was counted in parameters. Give an example of a
   one-parameter function with an enormous interface surface, and say what
   the real measure is.
4. The C++ probe measures rebuild time; the Rust probe measures a printed API
   list. Both are proxies. Name the kind of interface surface *neither* of
   them can see, and how you would catch it.

## Next up

[Topic 2 — Coupling and cohesion, measured](../02-coupling-cohesion-and-the-wrong-abstraction/README.md):
the same question asked of a whole repository rather than one module, using
your real git history as the measuring instrument.
