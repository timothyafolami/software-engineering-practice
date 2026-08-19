# Layer 8 · Topic 9 — Naming: the smallest interface there is

### The takeaway (read this first)

**The one idea:** a name is an interface. It is the only part of a module that
every reader consumes and most readers never look past, and its job is to make
a precise prediction about behaviour that the implementation then keeps. A
name you cannot write precisely is a design you have not finished — which
makes naming a *diagnostic*, not a decoration.

**Why it matters in practice:** naming is the one design activity that happens
hundreds of times a week, by everyone, with no review discipline attached to
it. It is also where two of this layer's earlier findings turn out to have
started: the shallow module in topic 1 is usually announced by a generic name,
and the pagination bug in topic 5 lives inside a parameter called `cursor`
that quietly hides which column it bounds and whether the bound is inclusive.

**You'll know it landed when:** you treat "I can't think of a good name for
this" as evidence about the *code* rather than about your vocabulary, and you
can say what information a name is carrying and what it is leaving to the
reader to guess.

**Why this topic exists at all:** naming is a named design bullet in the
source roadmap and had no coverage anywhere in this layer. It is numbered 9
rather than inserted after topic 1 so that topic numbers 1–8 stay stable with
the cross-references in `SEQUENCE.md` — but it belongs with topics 1 and 2,
and doing it alongside them works better than doing it last.

## The concept

A name has an information budget, and the reader's cost is whatever the name
does not spend. Three properties make the difference and all three are
checkable:

**Precision — does the name predict the behaviour?** `getUser(id)` predicts
almost nothing: does it return `None` or raise when there is no such user?
Does it hit the network? Does it cache? A precise name answers at least one of
those. `find_user(id) -> User | None` and `load_user(id) -> User` (raises) are
two different contracts, and choosing which verb you meant *is* the design
decision. The habit worth building: for every name you write, say out loud
which question it answers and which it leaves open. The ones it leaves open
have to be answered by the type, the docstring, or the reader.

**Consistency — is the same idea always called the same thing?** A codebase
with `get_`, `fetch_`, `load_`, `read_` and `find_` all meaning "one row by
id" charges every reader a translation table. This is the cheapest measurable
naming defect in any repository and the one nobody counts. It is also the one
that mechanised tooling can find without any judgement: it is a census, not a
review.

**Honesty — does the name describe what the thing *is*, or what you wish it
were?** `OrderManager`, `DataProcessor`, `utils.py`, `handle()`, `info` — these
are not names, they are placeholders that survived. Ousterhout's diagnosis is
sharper than "bad style": a generic name is usually a **symptom of a module
with no single responsibility**, because if it had one you could have named it.
This is the direct link to topic 1: a name like `service.py` and a shallow
pass-through layer are the same finding, seen from two angles.

Then the three claims that make naming a design tool rather than a style
guide:

1. **Write the interface comment before the implementation.** If the comment
   is hard to write — if it needs "and", or a list of cases, or a sentence
   about the caller's situation — the interface is wrong, and you have learned
   that before writing the body rather than after shipping it. This is the
   cheapest design review that exists and it takes about forty seconds.
2. **A name that needs "and" is two things.** This is topic 2's "same reason"
   test, made operational: if you cannot name the extracted shared function
   without a conjunction or a generic noun, the three call sites were not one
   concept and the abstraction is the wrong one.
3. **The name and the fix often arrive together.** Topic 5's `page(rows,
   cursor, limit)` is the case study. `cursor` states neither which column it
   bounds nor whether the bound is inclusive nor whether it is unique — and
   every one of those omissions is exactly where the bug lives. Rename it to
   `before_created_at` and the missing tie-handling becomes a question a
   reader asks unprompted. Rename it to what the *fixed* version needs —
   `before: tuple[created_at, id]` — and you have written the fix in the
   signature before touching the body.

The counter-argument deserves airtime, because this layer's recommended
reading contains both sides of it: Robert Martin's position is that a
sufficiently well-named small function makes comments unnecessary, and
Ousterhout's is that names cannot carry everything and comments are part of
the interface rather than an admission of failure. The
`aposd-vs-clean-code` debate is worth reading precisely here — the residual
disagreement is about how much information a name can carry before it becomes
`getUserByIdFromCacheOrDatabaseWithRetry`, and there is a real answer, which
is that the name carries the *concept* and the docstring carries the
*contract*.

## How each language actually gets there

Six languages, and the mechanism is concrete: **how much of the name the
language already supplies, and how much of the convention it enforces.** These
are not style preferences; they change how much information the identifier
itself has to carry.

**Go.** The reader always sees `package.Identifier`, so the package name is
part of every name and repeating it is an error, not a nicety —
`http.HTTPServer` reads badly, `http.Server` reads well. That composition is
why idiomatic Go names are famously short: `buf`, `n`, `err` are fine because
the scope is small and the qualification does the rest. The rule generalises
to every language: **name length should scale with scope**, and Go is where
that principle is visible in the language rather than in a style guide.

**Rust.** The naming conventions are documented API guidelines (RFC 430 and
the Rust API Guidelines) *and* they carry semantics. `as_` is a cheap
borrowed-to-borrowed conversion, `to_` is expensive, `into_` consumes the
receiver — so a reader learns the *cost* of a call from its prefix. `iter`,
`iter_mut`, `into_iter` are a family whose names encode ownership. Clippy
enforces a subset. This is the strongest example in the lab of names carrying
machine-checkable meaning, and it is worth stealing the idea even where the
tooling does not exist: pick a small verb vocabulary, write down what each
verb promises, and hold the codebase to it.

**Python (your stack).** Convention only: PEP 8 for shape, a leading
underscore for "not public", `__all__` for what a star-import sees. Nothing is
enforced, so the entire burden is on review — and reviewers reliably comment
on names being *ugly* and reliably fail to comment on names being *imprecise*.
The Python-specific hazard is that a module's name is chosen by the importer
(`import numpy as np`, `from .service import get as get_order`), so the
definition site's careful name can be discarded by anyone.

**Node.** The same importer-renaming problem, worse: `export default` means
the definition site supplies *no* name at all and every consumer invents one,
so the same function can be `getUser`, `fetchUser` and `u` in three files that
all import the same module. This is the strongest practical argument for named
exports, and it is a naming argument rather than a tooling one.

**Java.** The ceremony is the point and the trap. Long, fully-qualified,
explicit names are the norm, which makes precision cheap — and produces
`AbstractSingletonProxyFactoryBean`, where the name describes the
implementation's *pattern lineage* rather than what the thing does for a
caller. The one genuinely transferable Java habit: the checked convention that
a class name is a noun phrase and a method name is a verb phrase, mechanised
by Checkstyle, so that a method named `orderTotal` fails the build until it
becomes `computeOrderTotal` or `getOrderTotal` — a distinction the compiler
does not care about and every reader does.

**C++.** Almost no conventions and several competing ones (`snake_case` in the
standard library, `CamelCase` in most large codebases, Hungarian remnants in
older ones), plus two mechanisms that make names *load-bearing at compile
time*: namespaces, and argument-dependent lookup, where which function gets
called depends on the argument's namespace. C++ is the case that shows what
you get when the language enforces nothing: naming becomes a per-codebase
constitution that has to be written down, and the codebases that skipped
writing it are the unreadable ones.

## The experiment

**Two languages in the running experiment — Python and Go — and the one-line
reason: naming is a property of the reader rather than the runtime, so the
variable worth isolating is what the *module system* contributes to a name.**
Go and Python sit at opposite ends of that (mandatory package qualification
versus importer-chosen aliases) and the two of them produce the contrast.
Adding four more would produce four more style guides.

**1. The verb/noun census.** `tools/name_audit.py` walks the AST of `app/` (and
of your production service — this is the run that matters) and reports:

- every verb prefix in use, with counts, grouped by the shape of what the
  function returns, so that five verbs meaning "one row by id" show up as five
  rows next to each other;
- every noun that appears in more than one spelling (`order` / `purchase` /
  `txn`; `customer` / `user` / `account`);
- the census of generic names: `manager`, `handler`, `helper`, `util`,
  `process`, `handle`, `data`, `info`, `obj`, `tmp`;
- name length versus scope size (lines between definition and last use), which
  is the Go principle made measurable in Python.

**2. The blind-name quiz.** This is the topic's real measurement, and it needs
a week of delay to be honest. Take twenty public functions from your own
service. Show yourself **names and signatures only**, no bodies, no
docstrings, and answer three questions about each:

- What does it return when the thing does not exist?
- Does it mutate any argument?
- Can it raise, and if so with what?

Then check against the source and score. The percentage you got right is a
direct measurement of how much of the interface the names are carrying — and
because you wrote them, it is the most persuasive number in this layer.

**3. Rename the bug.** Take topic 5's `page(rows, cursor, limit)` in its
pre-fix state. Rename `cursor` to `before_created_at` and re-read the body
without running anything. Write down whether the tie bug is now visible by
reading. Then write the signature the *composite-cursor fix* requires —
`before: tuple[datetime, int] | None` — and note that you have now specified
the fix without implementing it.

**4. Comment-first, three times.** Pick three functions you are about to write
this week. Write the interface docstring first, before the body. Record how
many of the three required a *signature change* as a result — a parameter
dropped, a return type narrowed, a function split in two. That count is the
whole argument for the practice.

**5. The Go rewrite.** Port three of the worst-named Python functions to Go,
into a package whose name you also choose. Record the names you end up with.
The interesting outcome is not that they get shorter — it is which words moved
out of the function name and into the package name, and whether that
relocation would also have worked in Python as a module rename.

## How to run

Everything in this topic runs natively on macOS. No container, no database, and
nothing to install -- `name_audit.py` is standard library only.

```
python3 08-craft/lab/tools/name_audit.py --path 08-craft/lab/api/app
python3 08-craft/lab/tools/name_audit.py --path ~/path/to/your/service \
  --report verbs,nouns,generic,scope

python3 08-craft/lab/tools/name_audit.py --path ~/path/to/your/service --quiz 20 > /tmp/quiz.txt
# answer it, then -- the SAME seed draws the same twenty functions:
python3 08-craft/lab/tools/name_audit.py --path ~/path/to/your/service --quiz-key > /tmp/key.txt

cd 08-craft/lab/probes/go-naming && go doc -all . && go test ./...
```

The four reports are `verbs` (every verb prefix, grouped by the *shape* of what
the function returns, so synonyms land next to each other), `nouns` (declared
synonym families, checked -- a tool cannot discover that `txn` means `order`, it
can only check the families you write down, and writing them down is the useful
half), `generic` (the placeholder census), and `scope` (name length against lines
between definition and last use). The quiz prints names and signatures only: no
bodies, no docstrings. The key prints what each function actually raises and
whether it mutates an argument, and labels the mutation detector as a candidate
finder rather than a proof, because it is one.

`probes/go-naming` is experiment 5 -- three of the lab's worst-named Python
functions ported into a package whose name is half the exercise. The before/after
mapping is in the package doc comment, and the word that moved in every case is
`order(s)`: `orders_for_customer` becomes `orders.ForCustomer`,
`count_orders_for_customer` becomes `orders.CountForCustomer`, `recent_orders`
becomes `orders.Recent`. That relocation would also have worked in Python
(`from app import orders`); the reason it usually does not is that Python lets
the importer discard the module name, and Go does not offer that escape.

For experiment 3, the pre-fix `page(rows, cursor, limit)` and the fixed
`page_composite(rows, before, limit)` are both in
`08-craft/lab/api/app/core/pagination.py`, so the rename is a diff you can read
rather than one you have to imagine.

**Nothing in this topic is blocked on this machine.** `go doc -all .` and
`go test ./...` both pass, and the census runs against `lab/api/app` in under a
second. Note that the command is `go doc -all .`, not `go doc ./...` -- the
latter is not valid `go doc` syntax and exits 2 with a usage message.
## Predict, then record

Predict before running the census: how many distinct verbs do you think your
production service uses for "fetch one row by id"? How many spellings of your
core domain noun? And — the one people get most wrong — what percentage of the
blind-name quiz do you expect to answer correctly about code you wrote
yourself?

| Measure | lab `app/` | your production service |
|---|---|---|
| distinct verbs for "one row by id" | | |
| distinct spellings of the core domain noun | | |
| public functions with a generic name | | |
| longest name in the smallest scope | | |
| shortest name in the largest scope | | |

| Blind-name quiz (20 functions) | correct | wrong | unanswerable from the name |
|---|---|---|---|
| "returns what when missing?" | | | |
| "mutates an argument?" | | | |
| "can it raise, with what?" | | | |

| Comment-first | count |
|---|---|
| functions attempted | |
| docstrings that forced a signature change | |
| docstrings that needed the word "and" | |

**What would mean the experiment is broken rather than your prediction wrong:**

- **The census reports one verb and no synonyms.** Check that the audit is
  reading your whole tree and not a single package — and check that it is
  parsing rather than regex-matching, because a regex over `def ` will miss
  methods, decorated functions and anything defined in a class body.
- **You score near 100% on the blind quiz.** Either you took it too soon after
  writing the code (memory, not naming — wait a week, or use a colleague's
  module), or the quiz is showing you docstrings along with the signatures.
  Strip them; the whole point is to measure the names alone.
- **You score near 0%.** Check the sample. If the twenty functions were drawn
  from private helpers inside one module, low scores are expected and mean
  little — short local names in a small scope are *correct*. Re-draw from the
  public surface.
- **The rename makes no difference to how the bug reads.** Then you renamed
  the parameter but not the docstring or the call sites, so the reader still
  meets the old vocabulary first. Rename all three, then re-read.
- **The census flags names as generic that you can defend.** Not a defect —
  record the defence. `handler` in an HTTP framework's own vocabulary is a
  precise word. The audit produces candidates, and the judgement about which
  are real is the exercise.

## Answer before moving on

1. `find_user` returning `None` versus `load_user` raising: give a third
   contract, name it, and say which caller each of the three is right for.
2. Your census found five verbs for one operation. Write the migration plan
   you would actually propose — including what you would *not* rename, and
   why.
3. The blind-name quiz has an obvious objection: names are not supposed to
   carry the whole contract; types and docstrings exist. Steelman it, then say
   what the quiz is still measuring after you have granted the objection.
4. Topic 2's rule was "abstract when three cases are the same for the same
   reason." Explain how the naming test operationalises "same reason",
   construct a case where it gives the wrong answer — and say which Python
   change buys you Go's package qualification, and what it costs.

## Next up

That is the layer. Before you leave it, do the one thing that makes it stick:
take the single most important pure function in your production service — the
one whose bugs would be most expensive — and write **one property** for it.
Not a suite. One property. Run it with `max_examples=5000` and see what
happens.

Then [Layer 9 — Writing](../../09-writing/README.md), which follows this layer
for a reason: topic 2's coupling analysis and topic 7's latency ladder both
produce findings, and a finding nobody acts on may as well not exist.
