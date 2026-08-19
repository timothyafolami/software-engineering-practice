# Layer 9 · verification record

**Date:** 2026-08-19
**Verified by:** an independent pass. The code in this folder was written by
another agent; nothing here was taken on its word.

**What this file records:** that every program in `09-writing/` **executes on
this machine**, with the exact command its topic README gives, and that each one
fires on input it claims to catch. It records nothing about whether anything was
*learned*. The `Predict, then record` tables in the topic READMEs are still
blank, and they stay that way — they are the reader's exercise, and a filled-in
prediction table in a repository is a fabricated measurement.

---

## The machine

| | |
|---|---|
| OS | macOS 27.0 (build 26A5406e), Darwin |
| Architecture | arm64 (Apple silicon) |
| Python | 3.13.5 |
| Node.js | v24.14.0 |
| Go | go1.24.5 darwin/arm64 |
| Rust | rustc 1.97.1 |
| C++ | Apple clang 21.0.0, target arm64-apple-darwin27.0.0 |
| Java | javac / java 21.0.2 |
| git | present |
| Docker | CLI present, **daemon UP** (server 29.5.3, linux/aarch64, 4 CPU / 5.1 GB VM); Compose v5.1.4. Was DOWN on the first pass — see the unblock section |
| Postgres | `pg_isready` → `/tmp:5432 - accepting connections` |
| k6 | **not installed** |
| `timeout(1)` | not present on macOS — a `perl`-based wrapper was used to enforce the 60s rule |

**No Go, Rust, C++, Java or Node toolchain is invoked anywhere in this layer,
and that is correct rather than a gap.** Topic 6 is the only topic that claims
all six languages, and what it ships per language is a `commit-conventions.md`
and an installable `commit-template.txt` — which is what that topic's mechanism
actually is. The other topics' READMEs state plainly which languages they use
(Topics 1 and 4: Python; Topics 2, 3 and 5: none), and no program was invented
to fill a folder.

---

## Every program, and what happened

| # | Program | Command run (cwd) | Status |
|---|---|---|---|
| 1 | `lab/tools/sanitise_gate.py` | `python3 lab/tools/sanitise_gate.py` (`09-writing`) | **RAN** |
| 2 | `01-the-design-doc/tools/section-balance.sh` | `sh 01-the-design-doc/tools/section-balance.sh [file]` (`09-writing`) | **FIXED-THEN-RAN** |
| 3 | `02-rejected-alternatives/tools/flip-check.sh` | `sh 02-rejected-alternatives/tools/flip-check.sh worked-example/version-{a,b}.md` (`09-writing`) | **RAN** |
| 4 | `03-the-rfc-loop/tools/rfc-check.sh` | `sh 03-the-rfc-loop/tools/rfc-check.sh '' 03-the-rfc-loop/worked-example-ledger.md` (`09-writing`) | **RAN** |
| 5 | `04-the-postmortem/python/timeline_from_logs.py` | `python3 python/timeline_from_logs.py` (`09-writing/04-the-postmortem`) | **RAN** |
| 6 | `04-the-postmortem/python/postmortem_check.py` | `python3 python/postmortem_check.py worked-example.md` (`09-writing/04-the-postmortem`) | **RAN** |
| 7 | `05-explaining-tradeoffs/tools/jargon-check.sh` | `sh tools/jargon-check.sh worked-example/three-sentences.md` (`09-writing/05-explaining-tradeoffs`) | **RAN** |
| 8 | `06-commits-and-prs/tools/archaeology.sh` | `sh <abs>/tools/archaeology.sh` and `… app.py 3 3` (inside a throwaway git repo) | **RAN** |
| 9 | `07-writing-publicly/tools/scarcity-check.sh` | `sh 07-writing-publicly/tools/scarcity-check.sh 07-writing-publicly/worked-example.md` (`09-writing`) | **FIXED-THEN-RAN** |
| 10 | `06-commits-and-prs/{python,nodejs,golang,rust,cpp,java}/commit-template.txt` | `git config commit.template <abs path>` then `git commit` | **FIXED-THEN-RAN** |

Nothing is BLOCKED. Nothing hung: the slowest program is
`timeline_from_logs.py`, which waits at most `STDIN_WAIT = 1.5s` for piped input
and then falls through to its labelled demo. Every run above finished in under a
second of wall clock apart from that wait.

### One input path that could not be exercised on the first pass

Topic 4's README offers `docker compose logs --no-color --since 72h api |
python3 python/timeline_from_logs.py` as one of three ways to feed the timeline
tool. The Docker daemon was down when this file was first written, so that
*pipe* was not exercised. It has since been run for real — see **Unblock pass**
below. It was never a blocked program: the same tool was verified through its
other two input paths (file arguments, and a shell pipe carrying mixed ISO-8601
/ common-log / syslog lines) plus the no-input demo.

## Fixes applied during verification

**1. `01-the-design-doc/tools/section-balance.sh` printed a command that could
not be run from anywhere.** With no drafts present, its help text emitted:

```
  sh tools/section-balance.sh 01-the-design-doc/worked-example.md
```

That mixes two working directories — the relative script path assumes cwd is
`01-the-design-doc/`, the file argument assumes cwd is `09-writing/`. From
`09-writing` it fails with `sh: tools/section-balance.sh: No such file or
directory`; from `01-the-design-doc` it prints `skip (not a file)`. Rewritten to
the `09-writing`-rooted form used by the README and by the sibling tools, with
the cwd stated. Re-run: correct.

**2. The six `commit-template.txt` files told you to install them with a
relative path.** Each ended with:

```
# Install:  git config commit.template 09-writing/06-commits-and-prs/<lang>/commit-template.txt
```

`git` resolves `commit.template` against the working directory at commit time,
and the documented workflow for this topic is to `cd` into *your production
service repo* first — where that relative path resolves to nothing and the
template silently never appears. The topic README already used an absolute
`$LAYER9/...` form; the in-file comments now say the path must be absolute and
why. Verified end to end: `git config commit.template <abs>` in a throwaway repo
followed by `git commit` opened the editor pre-filled with the template body.

**3. `07-writing-publicly/tools/scarcity-check.sh` described its own filter
inaccurately.** It printed "lines whose numbers sit inside `<placeholders>` are
filtered out", but the filter (`grep -vE "^[0-9]+:.*<[^>]*>"`) drops any line
containing a placeholder *anywhere* — so a sentence carrying both a placeholder
and a real, unsourced numeral is hidden from the very list that exists to catch
it. The filter is left alone (it is deliberately a crude proxy); the note now
states the actual behaviour and tells the reader to re-read the placeholder
lines by hand. In a layer whose first rule is "never write a number you did not
measure", a checker that quietly under-reports numbers had to say so.

---

## Does each tool test its actual claim?

This is the check Layer 1 failed, so each tool was run against input designed to
break it, not only against the worked example it was tuned on.

| Tool | Claim | Negative control | Result |
|---|---|---|---|
| `section-balance.sh` | fails a doc whose Proposed design outweighs Context+Alternatives by more than 2:1 | synthetic doc with a 300-word proposal and a 7-word Context+Alternatives | `RATIO 42.86:1 <- rubric 7 FAILS`; a doc with no matching headings reports `n/a` and names the reason rather than dividing by zero |
| `flip-check.sh` | version A fails every block, version B passes every block | the two worked versions, as the README states | A: 3 of 3 FAIL, plus `MISSING: "do nothing"`. B: 4 of 4 ok. The claimed contrast is real |
| `rfc-check.sh` | catches a missing header block and ledger rows that land nowhere | RFC with no Status/Reviewers/decision date; ledger with an empty "where it landed" and an empty reason | all five header checks fired; both bad rows reported; the worked ledger reports 6 rows, 0 problems |
| `timeline_from_logs.py` | reproduces timestamps verbatim, refuses to guess a start, labels synthetic input | mixed ISO-8601 + common-log + syslog through a pipe, through file args, and with no input at all | timestamps echoed unmodified; year-less syslog rows segregated under an explicit "would mean assuming a year" note; the demo prints a 4-line banner saying the lines are made up |
| `postmortem_check.py` | catches counterfactuals, person-shaped factors, weak actions, unsourced numbers, missing clocks | a deliberately bad postmortem | 14 problems found across all five categories, exit 1. On `worked-example.md`: 13 unknowns, 0 problems, exit 0 |
| `jargon-check.sh` | zero hits on the worked example, and every hit carries its conversion | three jargon-dense sentences | 12 terms flagged across 3 lines, each with its "say instead" line. Worked example: 0 hits, as claimed |
| `archaeology.sh` | counts per **commit**, not per line | a 3-commit repo with exactly one non-empty body | `1 of 3` non-empty bodies, 1 naming a rejected path, 1 trailer — per-commit, correct. Retrieval mode printed both commits touching the line with full bodies |
| `sanitise_gate.py` | catches the mechanical half of the checklist, and prints the un-checkable half every run | a draft containing a hostname, a private IP, an email, a ticket key, a currency figure, an on-call line and an internal endpoint | all 7 built-in rule classes fired, exit 1; local `sensitive-patterns.txt` loading and its invalid-regex path also verified |
| `scarcity-check.sh` | lists condition classes then every numeral | the worked example | `4 of 5 condition classes present` (architecture not stated — a true finding about that example) |

No mismatch of the Layer 1 kind — a program whose behaviour contradicts the
sentence describing it — was found in this layer.

---

## Honesty checks

- **All 124 relative markdown links across the layer resolve**, including the
  cross-layer ones into `01-machine/03-concurrency-models/`,
  `06-observability/lab/` and `08-craft/01-deep-and-shallow-modules/`. Checked
  by resolving every inline-link target against the filesystem, not by reading.
- **Every `Predict, then record` table in all seven topic READMEs is blank**,
  and so are both tables in `log.md` and the three-clock table in Topic 4's
  README. None was filled; none needed blanking.
- **No fabricated measurement anywhere.** Grepping every `.md` for numerals
  attached to units (`ms`, `ns`, `%`, `req/s`, …) outside `<placeholder>`
  brackets returns nothing. Every figure in every worked example is a
  placeholder with its source slot beside it.
- **The one real number in the layer is borrowed, and says so.** Topic 7's
  worked example cites Layer 1's optimiser defect — the C++/Rust "0 lost
  updates" that was the optimiser hoisting the increment out of the loop — as
  recorded provenance, pointing at the root `README.md` and `PREDICTIONS.md`
  where it is logged, and attaches the correct warning in bold: *it is not yours
  until you rerun it*, with your compiler version and your build flags. That is
  the right way to reference someone else's measurement and it survives review.
- **`artifacts/` is still empty** apart from `.gitkeep` files, which is correct:
  those seven folders are for what the reader writes. Verification created no
  drafts in them. Test fixtures were written to a scratch directory and the one
  file this pass added under `lab/tools/` (`sensitive-patterns.txt`, to exercise
  the local-pattern loader) was removed afterwards; only the committed
  `.example` file remains.

## Coverage

All seven topics have a full README, a worked example, a rubric-as-checks file,
and at least one runnable tool. `topicsIncomplete` is empty.

One observation rather than a defect, recorded so a later reader does not have
to rediscover it: Topic 1's `How each language actually gets there` section says
"this topic uses one: Python", while the tool it ships is POSIX `sh`. There is
no contradiction — that section is about the *subject* of the artifact (a doc
about a FastAPI/Postgres service), and it says explicitly that the mechanism
"lives entirely outside any runtime". Topics 2, 3, 5 and 7 declare "uses none"
and ship shell tools on the same basis. The shell scripts throughout this layer
are lab tooling, not the language treatment, and reading them as a narrowing of
the six-language claim would be a misreading.

## Portability

All shell is POSIX `sh` with the `awk`, `grep` and `sed` that ship with macOS —
no GNU-only flags, verified by running rather than by reading. No `epoll`,
`/proc`, or cgroup dependency exists anywhere in this layer, because no program
here touches the kernel. `mktemp -t` (used by `jargon-check.sh`) is the BSD
form and works here. `timeout(1)` is absent on macOS but no tool in this layer
requires it.

---

## Unblock pass — Docker daemon up

**Date:** 2026-08-19 (second pass)
**What changed on the machine:** the Docker daemon is now running — server
29.5.3, `linux/aarch64`, 4 CPUs / 5.1 GB in the VM, Compose v5.1.4. Postgres is
up on `:5432`. `k6` is still not installed, and nothing in this layer wants it.

**There was nothing marked BLOCKED in this layer to unblock.** The first pass
recorded "Nothing is BLOCKED", and re-reading the file confirms it: all ten
programs already ran. What the daemon coming up made possible was the one
*input path* the first pass could not exercise — Topic 4's `docker compose logs`
pipe. That has now been run for real, and all ten programs were re-run to
confirm they still execute.

### The Docker input path, now exercised

No compose stack ships in `09-writing` (`lab/README.md` says so plainly: this
layer shares a filing convention, not a stack). So a throwaway stack with a
service literally named `api` was written to a scratch directory outside the
repo, brought up under its own project name, piped through the tool, and torn
down with `docker compose down -v`. Nothing was added to the repository.

| Command | Result |
|---|---|
| `docker compose -p writing-postmortem logs --no-color --since 72h api \| python3 python/timeline_from_logs.py` | **RAN.** `lines read: 15`, `with a timestamp: 13`, `distinct event shapes: 5`, `formats seen: iso8601` |
| `docker compose -p writing-postmortem logs --since=72h api \| head -50` (README's other Docker line) | **RAN.** Emits the prefixed stream the first command consumes |
| the same pipe with a second service (`db`, `postgres:17-alpine`) in the stack | **RAN.** `lines read: 71`, `with a timestamp: 31`, `distinct event shapes: 20` |

**The claim being tested was `COMPOSE_PREFIX`.** `timeline_from_logs.py` line 82
carries `COMPOSE_PREFIX = re.compile(r"^[A-Za-z0-9_.-]+\s*\|\s")` with the
comment *"docker compose prefixes every line with `service-1  | `"*. Until now
that regex had only ever been tested against a hand-typed imitation of a compose
prefix. Real compose output pads the service name to a common width, so a
two-service stack emits **`api-1  | `** (two spaces) and **`db-1   | `** (three).
Both were stripped correctly: no row in the generated table carries a service
prefix in its "What happened" column, and none collapsed into a shape like
`api-<n> |`. The comment is accurate and the regex handles the padding.

Timestamps came through verbatim, as claimed — the ISO-8601 recogniser matched
Postgres's space-separated `2026-08-19 14:02:24.297` as well as the API's
`2026-08-19T13:59:43.415Z`, with no normalisation applied to either. The two
deliberately untimestamped lines in the fixture (`Starting api service`,
`shutdown complete`) were counted as untimestamped rather than guessed at:
15 lines read, 13 with a timestamp.

**One cosmetic observation, not a defect.** Postgres logs its timezone *after*
the timestamp (`2026-08-19 14:02:24.297 UTC [1] LOG: …`), and the ISO recogniser
stops before the ` UTC`, so that token leads the message column: `UTC [<n>] LOG:
listening on …`. This is the tool doing exactly what its docstring promises — it
"does not normalise or convert timestamps" and reproduces only what it matched —
and the leftover `UTC` is arguably useful, since the same docstring tells you to
"check they are UTC yourself". Left alone.

### The `psql` line in the same README block — fixed

The first pass set this aside as "a suggestion aimed at the reader's own
production database". With Postgres now reachable it was run anyway, and it
fails on a stock server:

```
ERROR:  relation "pg_stat_statements" does not exist
```

`pg_stat_statements` is *available* here (`pg_available_extensions` lists it)
but not installed, and `shared_preload_libraries` is empty — so it cannot be
enabled without editing the server config and restarting. A reader copying that
line onto a fresh Postgres gets the error above and no hint why. In a layer
whose commands are meant to be runnable, that needed a prerequisite note, so one
was added above the command in
[`04-the-postmortem/README.md`](04-the-postmortem/README.md): the extension must
be in `shared_preload_libraries` (a server restart) *and* created in the
database, and the exact error is quoted. The query itself is unchanged — it is
still aimed at your production database, where a DBA has usually enabled it.

### Re-run of all ten programs

Every program in the table above was re-run after the daemon came up. All ten
still execute, all with the same exit statuses recorded on the first pass —
including `archaeology.sh` and the six `commit-template.txt` files, re-verified
in a fresh throwaway git repo (`git config commit.template <abs>` followed by
`git commit --allow-empty` with `GIT_EDITOR=cat` printed the template body).

### Still blocked

**Nothing.** This layer has no remaining blocked entry. It never depended on
Docker, cgroups, `/proc`, `epoll` or a load generator, because no program in it
touches the kernel or a network — the Docker pipe was one of three documented
ways to feed a single text-processing tool, and it now works. `k6` remains
uninstalled and remains irrelevant here.

### Teardown

`docker compose -p writing-postmortem down -v` — containers and network removed,
confirmed by `docker ps -a`. The scratch stack lives outside the repository and
no file under `09-writing/` was added by this pass; the only change is the
prerequisite comment in Topic 4's README described above.
