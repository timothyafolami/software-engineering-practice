# Layer 7 · Topic 7 — Secrets and supply chain: the code you didn't write runs on your machine

### The takeaway (read this first)

**The one idea, part A (secrets):** a secret in source control is
compromised the moment it is pushed, and it stays compromised in git history
after you delete the line — so the only real remediation is **rotate**, not
delete. **Part B (supply chain):** installing a dependency *runs that
dependency's code*. `npm install` executes lifecycle scripts, `pip install`
from an sdist runs `setup.py`, `cargo build` runs `build.rs`, Gradle
evaluates a build script that is a program. Your build machine and your CI
trust every transitive package you pull, at install time, before any of your
tests run. In the OWASP Top 10:2025 this graduated to its own category:
**A03, Software Supply Chain Failures.**

**Why it matters in practice:** the npm **Shai-Hulud** worm (September 2025
onward) is the concrete case to study. Its mechanism is the whole lesson in
one sentence: a compromised package's post-install script harvested
credentials from the machine that installed it, then used those credentials
to publish trojanised versions of *that maintainer's own* packages — a
self-replicating supply-chain compromise, spreading through exactly the
install-time code execution described above. Read the npm and GitHub
advisories for the scope; **this file deliberately quotes no counts.** An
earlier draft carried four memorable statistics about secret sprawl with no
citation anywhere, which is the same defect as a pre-filled results table,
just harder to spot. If you want the annual numbers, GitGuardian's *State of
Secrets Sprawl* is the report that produces them — go read the current one
and cite it yourself.

**You'll know it landed when:** you have a specific, ordered runbook for "we
leaked a secret" that starts with rotate and not with `git rebase`, and you
can explain why a lockfile with hashes is a security control and not just a
reproducibility one.

## The concept

### Secrets

Everything derivable from one fact — *anything in the repository is
available to everyone with repository access, forever, including history and
including forks*:

- Keep secrets out of source: env vars, a secret manager, or better, no
  long-lived secret at all.
- Scope each secret to the least it needs — the blast radius of a leak is
  decided at issue time, not at leak time.
- Rotate on a schedule *and* the instant one leaks.

**The leak runbook, in this order:**

1. **Rotate/revoke first.** The leaked value is burned. Assume automated
   scrapers found the push before you did — treat the interval as zero
   rather than as a number you can reason about.
2. *Then* purge from history (`git filter-repo`, and remember forks and
   mirrors and every CI cache) and turn on push protection.
3. **Audit** for use of the credential in the window it was live. This is
   the step people skip, and it is the one that tells you whether you had an
   incident or a near miss.
4. Close the path that put it there.

Note that steps 1 and 2 are commonly performed in the wrong order, and the
reason is psychological rather than technical: scrubbing the history feels
like fixing it, and rotation feels like paperwork. The ordering is forced by
one assumption — that the attacker already has the value — and if you do not
believe that assumption, you will keep getting the order wrong.

The direction of travel is to stop having long-lived secrets: **OIDC-minted,
short-lived credentials** (a CI job authenticating to a cloud provider or a
package registry with a token minted per run and valid for minutes) removes
the thing that leaks. That is the same move as Topic 4's short TTLs, applied
to machines.

### Supply chain

`install` is code execution. The defences, in order of how much they buy:

- **Lockfiles with hashes.** A version pin says "give me 1.4.2"; a hash pin
  says "give me *this artifact*." Those are different claims, and only the
  second one survives a maintainer's account being taken over and 1.4.2
  being republished with new bytes. For Python this is finally standardised:
  **PEP 751** defines `pylock.toml` (approved 2025), producible by `uv`, pip
  25.1+, and PDM; `uv.lock` remains uv's richer cross-platform default and
  `pylock.toml` is the interoperable export.
- **Pin versions, and add a cooldown.** Do not auto-adopt a release in its
  first hours — that window is when a compromised republish is live and not
  yet caught. pnpm shipped release-cooldown and lifecycle-script blocking as
  defaults in late 2025, in direct response to Shai-Hulud.
- **Disable install scripts** where you can (`npm install --ignore-scripts`,
  pnpm's default-deny), understanding the tradeoff: some packages genuinely
  need them to build native extensions, and the honest posture is an
  allowlist of the few that do.
- **Verify provenance.** Trusted publishing — the registry accepts a build
  from a CI workflow it can verify, rather than a token someone stored —
  replaces the long-lived publish token that Shai-Hulud existed to steal.

## How each language actually gets there

All six, and this is the topic where the six ecosystems differ most
sharply — because the question "what code runs when I add a dependency"
has six genuinely different answers.

**Python (`uv` / pip).** An **sdist** runs `setup.py` at install time,
arbitrary code, as you. A **wheel** does not — it is unpacked, not executed
— which makes "wheels only" a real, under-used control (`pip install
--only-binary :all:`). `uv` with a committed lockfile plus hash verification
is the current posture; keep secrets in the environment via
`pydantic-settings` and never commit `.env`.

**Node (npm).** The canonical case: `preinstall`, `install` and
`postinstall` run for every dependency in the tree, transitively, before you
have run a single line of your own code. `npm ci` against a committed
`package-lock.json` fixes the *versions and integrity hashes*;
`--ignore-scripts` fixes the *execution*. They are separate controls and you
need both.

**Go (modules).** The interesting contrast: the module system fetches
**source only** and runs **no install-time scripts at all**, `go.sum` hashes
every module, and by default the toolchain verifies against a public
**checksum database** — so a republished version with different bytes is
rejected by a third party's log, not just by your local file. Go
structurally does not have the install-executes-code problem. It absolutely
still has the *dependency* problem: the code runs when you run your program.

**Rust (Cargo).** Do not let Go's neighbourhood fool you into assuming
compiled languages are safe here: `build.rs` is a Rust program that Cargo
**compiles and executes on your machine at build time**, with your
permissions, and procedural macros execute at *compile* time inside the
compiler. `Cargo.lock` carries hashes and `cargo vet`/`cargo-deny` exist
precisely because the execution surface is real. Rust's compile-time safety
story, so central in Topics 2 and 4, offers nothing here — a useful
correction to "Rust is the safe one."

**Java (Maven / Gradle).** Maven executes **plugins** during the build
lifecycle, which are ordinary Java artifacts resolved from the same
repositories as your dependencies. Gradle goes further: `build.gradle(.kts)`
*is* a program, and any plugin it applies runs at configuration time. Java's
mitigating feature is the strongest artifact-verification story of the six —
signed artifacts and checksums have been Maven Central policy for a long
time — and its weakness is that verifying them is opt-in.

**C++ (vcpkg / Conan).** The most direct: a vcpkg portfile is a CMake script
and a Conan recipe is a **Python file**, both executed on your machine to
fetch and build. There is no sandbox, and the ecosystem norm of building
from source means arbitrary code execution at install is not an edge case,
it is the design. The lesson from placing C++ last: every abstraction the
other five put between you and "run this stranger's build script" is a
convenience, and the underlying operation was always the same one.

## The experiment

Two parts. Both run locally; neither needs the compose stack.

**Part A — secrets.** A scratch repository under `lab/secrets-drill/` with a
planted, realistically-formatted fake credential (an `AKIA`-shaped string,
not `SECRET=hunter2` — scanners match patterns, and a fake that matches no
pattern teaches you that scanners are useless rather than that yours is).

1. Run `gitleaks detect` and record **findings count** and **scan seconds**.
2. `git rm` the line, commit, re-run the scanner on the working tree
   (clean) and on history (`--no-git` vs default). Record **which commits
   still contain it**.
3. Rehearse the runbook against a throwaway credential you actually own,
   and time it: **seconds from "noticed" to "rotated"**. That number is the
   only one that matters during a real incident, and almost nobody has
   measured theirs.
4. Enable push protection and attempt a new push containing a secret.
   Record whether it is blocked and what the error says.

**Part B — install-time execution.** `lab/evil-package/` is a local package
whose `postinstall` writes a marker file with a timestamp — a benign
stand-in for "harvest every token in the environment and POST it somewhere."

1. `npm install ./evil-package` — record whether the marker exists and
   **how many seconds before the command returned** it was written (the
   answer to "did this run during install or when I imported it").
2. `npm install ./evil-package --ignore-scripts` — record marker absence.
3. Python: install the same idea as an **sdist**, then as a **wheel**,
   and record which one produced a marker. This is the comparison most
   Python developers have never made.
4. Generate `uv.lock` and export `pylock.toml`. Tamper with one hash, run
   `uv sync`, and record the **exact exit code and message** — warn or
   refuse.

### How you'd know the fix is fake

`--ignore-scripts` in your terminal and not in CI. The control lives in the
place the install actually happens, and the machine with the credentials
worth stealing is the CI runner, not your laptop. Similarly, a `.gitignore`
entry for `.env` is not a secrets control — it prevents an accident, and
prevents nothing at all once the file is already tracked. The check that
distinguishes real from theatrical: `git log --all -p -- .env` and
`npm config get ignore-scripts` **on the runner**.

## How to run

**Part A — secrets.** `gitleaks` is not installed on this machine, so
`python/secret_scan.py` is the self-contained stand-in: it plants an
AKIA-shaped fake credential, scans the working tree, then commits it, `git
rm`s it, and re-scans — proving the credential survives in history after
removal.

```
python3 python/secret_scan.py
```

**Part B — install-time execution**, one runnable fixture per ecosystem that
supports it offline. Each writes a benign marker so you can prove *when* the
code ran:

```
# Node — postinstall runs before your code; --ignore-scripts stops it:
rm -f /tmp/pwned.txt
npm install ./nodejs/evil-package                  && ls -l /tmp/pwned.txt
rm -f /tmp/pwned.txt; npm install ./nodejs/evil-package --ignore-scripts; \
  ls /tmp/pwned.txt 2>/dev/null || echo "no marker: scripts skipped"

# Python — sdist runs setup.py, wheel does not (see python/README-run.md):
rm -f /tmp/pwned-py.txt
pip install --no-build-isolation ./python/evil-sdist && cat /tmp/pwned-py.txt

# Rust — cargo compiles and runs build.rs at BUILD time:
rm -f /tmp/pwned-rs.txt
cd rust/evil-build && cargo build && ls -l /tmp/pwned-rs.txt && cd ../..

# Go — the instructive contrast: no install-time execution at all:
cd golang && go run supply_chain.go && cd ..
```

The uv hash-tamper control is in `python/uvlock_tamper.md`. The Java (Gradle)
and C++ (vcpkg/Conan) fixtures — `java/build.gradle.kts`, `cpp/portfile.cmake`,
`cpp/conanfile.py` — are the real executable build scripts, blocked here only
because Gradle/vcpkg/Conan are not installed; each file's header shows the one
command that runs it. The `.gitignore`-is-not-a-control and
`--ignore-scripts`-must-be-on-the-runner checks (`git log --all -p -- .env`,
`npm config get ignore-scripts`) are the fake-fix tells.

## Predict, then record

1. After `git rm` and commit, is the secret still retrievable? From where,
   with which command? What does that imply about "delete the commit" as
   remediation?
2. Does the `postinstall` marker appear before or after `npm install`
   returns — i.e. did the code run during install, or only when you *use*
   the package? Predict the same for the Python sdist and for the wheel, and
   note that one of those three differs from the other two.
3. With a tampered hash in the lockfile, does `uv sync` warn or refuse?
   Predict the exit code.

| Secrets measurement | value |
|---|---|
| `gitleaks` findings, before deletion |  |
| `gitleaks` findings on working tree, after deletion |  |
| commits still containing the secret, after deletion |  |
| push protection: blocked? (exit code + message) |  |
| your measured seconds from "noticed" to "rotated" |  |

| Install-time execution | marker written? | marker mtime vs command exit | install seconds |
|---|---|---|---|
| `npm install` |  |  |  |
| `npm install --ignore-scripts` |  |  |  |
| `pip install` sdist |  |  |  |
| `pip install --only-binary :all:` (wheel) |  |  |  |

| Lockfile | value |
|---|---|
| `uv sync` exit code with a tampered hash |  |
| packages in the tree that declare an install script (count) |  |
| transitive dependency count for the same tree |  |

The last two rows are the ones to sit with: the ratio between them is your
actual install-time attack surface, and it is a number about *your* project
that no article can tell you.

**What would mean the experiment is broken, not the prediction:** if
`gitleaks` finds nothing, your planted key does not match a known pattern —
use a realistically-shaped fake. If the `postinstall` marker never appears,
scripts may be globally disabled in your config (`ignore-scripts=true` in
`.npmrc`); that is the mitigation working, so run `npm config get
ignore-scripts` before concluding the attack failed. If `pip install` of the
sdist produces no marker, pip may have built and cached a wheel from it on
an earlier run — clear the cache, because a cached wheel means the code
already ran once and you missed it. If `uv sync` accepts a tampered hash
silently, you are not in a hash-checking mode; confirm the lockfile carries
hashes at all.

## Answer before moving on

1. Why is "rotate" the first step of leak remediation and not "scrub git
   history"? Name the assumption about the attacker that forces the
   ordering, and say what would have to be true for the reverse order to be
   acceptable.
2. A lockfile pins versions *and* hashes. Which of those two is the
   supply-chain security control, and what specific attack does the *hash*
   stop that the *version pin* alone does not?
3. Go runs no install scripts and npm does. Does that make Go dependencies
   safe to add freely? Name the attack surface that is identical in both
   ecosystems regardless of install-time execution.
4. Rust and Go are both compiled, both "safe" languages, and they sit on
   opposite sides of this topic. Explain why, in terms of what each
   toolchain does between `add dependency` and `run tests`.

## Next up

[Topic 8 — Crypto hygiene and rate limiting](../08-crypto-and-rate-limiting/README.md):
the last topic, and the one where the defence is a measurement — how long a
comparison takes, and how many attempts an attacker gets.
