# Layer 7 · 07-security — independent verification

**Verified:** 2026-08-19 (independent pass; code was written by another agent and not trusted).

**What this records:** that each program **executes** and prints the output shape
its README claims, on this machine, with the exact command in its topic README.
It does **not** record that any lesson was learned — the `Predict, then record`
tables remain blank and are the reader's exercise.

## The machine

- macOS 27.0 Darwin, arm64 / Apple M1
- Python 3.13.5 (jinja2 3.1.6, PyJWT 2.10.1, argon2-cffi present)
- Node 24.14.0 (`node:sqlite` built-in used by Topic 2; React not in offline cache)
- Go 1.24.5 darwin/arm64 (module caches present for pgx, golang-jwt/v5, x/crypto)
- Rust/cargo 1.97.1 (sqlx / jsonwebtoken / sha2 / argon2 crates NOT cached)
- Apple clang 21.0.0 (`g++`); Homebrew `libpq` and `openssl@3` present
- JDK 21.0.2 (`javac`/`java`); PostgreSQL JDBC driver jar NOT present
- **Postgres reachable** — `pg_isready` → accepting connections on :5432 (Topics 1, 2 use it)
- **Docker daemon UP** (as of the 2026-08-19 unblock pass) — server 29.5.3, linux/aarch64,
  4 CPU / 5.1 GB VM. Used to run Gradle / Conan / cmake in throwaway containers.
- **Network available** (as of the unblock pass) — npm registry and crates.io reachable,
  so the cargo/npm/JDBC-jar fetches that were offline-blocked now succeed.
- **k6 not installed** (still); Gradle / Conan / vcpkg / cmake still not on the host
  (run in Docker containers instead of installing them)

`timeout(1)` is absent on macOS; every run was wrapped in a 40–120 s kill-guard.
No hangs: the longest single program (Go blind-extraction / first-compile) finished
well under 60 s of its own runtime.

## Program-by-program status

| Topic | Lang | Command (from topic README) | Status |
|---|---|---|---|
| 01 idor | Python | `python3 python/idor_enumeration.py` | RAN |
| 01 idor | Python (PG) | `python3 python/idor_rls_postgres.py` | RAN (real Postgres) |
| 01 idor | Node | `node nodejs/idor_enumeration.js` | RAN |
| 01 idor | Go | `cd golang && go run idor_enumeration.go` | RAN |
| 01 idor | Java | `javac IdorEnumeration.java -d /tmp/t1java && java -cp /tmp/t1java IdorEnumeration` | RAN |
| 02 sqli | Python | `python3 python/sqli.py` | RAN (real Postgres) |
| 02 sqli | Node | `node nodejs/sqli.js` | RAN (node:sqlite; ExperimentalWarning, exit 0) |
| 02 sqli | Go | `cd golang && GOFLAGS=-mod=mod GOPROXY=off go run sqli.go` | RAN (real Postgres) |
| 02 sqli | C++ | `g++ -O2 -std=c++17 -I$LIBPQ/include -L$LIBPQ/lib -o /tmp/cpp_sqli cpp/sqli.cpp -lpq && /tmp/cpp_sqli` | RAN (real Postgres) |
| 02 sqli | Rust | `cd rust && cargo fetch && cargo run` | RAN (real Postgres; blind extraction 1258 requests) |
| 02 sqli | Java | `javac Sqli.java -d /tmp/t2java && curl -L -o /tmp/pg.jar https://jdbc.postgresql.org/download/postgresql-42.7.4.jar && java -cp /tmp/t2java:/tmp/pg.jar Sqli` | RAN (real Postgres; blind extraction 1258 requests) |
| 03 xss | Python | `python3 python/xss_context.py` | RAN (Jinja2: 2/4 execute autoescape-on, 4/4 off) |
| 03 xss | Go | `cd golang && go run xss_context.go` | RAN (html/template: 0/4) |
| 03 xss | Node/React | `cd nodejs && npm install && node xss_context.js` | RAN (react/react-dom installed; 3/4 payloads execute) |
| 04 jwt | Python | `python3 python/jwt_demo.py` | RAN |
| 04 jwt | Node | `node nodejs/jwt_demo.js` | RAN |
| 04 jwt | Go | `cd golang && GOFLAGS=-mod=mod GOPROXY=off go run jwt_demo.go` | RAN |
| 04 jwt | Java | `javac JwtDemo.java -d /tmp/t4java && java -cp /tmp/t4java JwtDemo` | RAN (JDK crypto) |
| 04 jwt | Rust | `cd rust && cargo fetch && cargo run` | RAN (alg-confusion REJECTED; revocation plain 1500ms vs denylist 0ms) |
| 05 oauth | Python | `python3 python/pkce_flow.py` | RAN |
| 05 oauth | Node | `node nodejs/pkce_flow.js` | RAN |
| 05 oauth | Go | `cd golang && go run pkce_flow.go` | RAN |
| 06 ssrf | Python | `python3 python/ssrf.py` | RAN |
| 06 ssrf | Node | `node nodejs/ssrf.js` | RAN |
| 06 ssrf | Go | `cd golang && go run ssrf.go` | RAN |
| 06 ssrf | Java | `javac Ssrf.java -d /tmp/t6java && java -cp /tmp/t6java Ssrf` | RAN |
| 06 ssrf | C++ | `g++ -O2 -std=c++17 -o /tmp/cpp_ssrf cpp/ssrf.cpp && /tmp/cpp_ssrf` | RAN |
| 06 ssrf | Rust | `cd rust && cargo run` | RAN (std::net only, offline) |
| 07 supply | Python (scan) | `python3 python/secret_scan.py` | RAN (temp git repo, auto-cleaned) |
| 07 supply | Node fixture | `npm install ./nodejs/evil-package` (+ `--ignore-scripts`) | RAN — marker written; suppressed with `--ignore-scripts` |
| 07 supply | Python sdist | `pip install --no-build-isolation ./python/evil-sdist` | RAN — setup.py executed at build, marker written |
| 07 supply | Rust build.rs | `cd rust/evil-build && cargo build` | RAN — build.rs executed at build (clean build), marker written |
| 07 supply | Go | `cd golang && go run supply_chain.go` | RAN |
| 07 supply | Java/Gradle | `docker run --rm -v "$PWD":/w -w /w gradle:8.10-jdk21 gradle help` | RAN (in container; config-time code executed) |
| 07 supply | C++/vcpkg+Conan | Conan+cmake in throwaway containers (see append) | RAN (recipe + portfile both executed) |
| 08 crypto | Python | `python3 python/crypto_ratelimit.py` | RAN (argon2id) |
| 08 crypto | Node | `node nodejs/crypto_ratelimit.js` | RAN (scrypt) |
| 08 crypto | Go | `cd golang && GOFLAGS=-mod=mod GOPROXY=off go run crypto_ratelimit.go` | RAN (argon2id) |
| 08 crypto | Java | `javac CryptoRateLimit.java -d /tmp/t8java && java -cp /tmp/t8java CryptoRateLimit` | RAN (PBKDF2) |
| 08 crypto | C++ | `g++ -O2 -std=c++17 -I$SSL/include -L$SSL/lib -o /tmp/cpp_crl cpp/crypto_ratelimit.cpp -lcrypto && /tmp/cpp_crl` | RAN (PBKDF2, CRYPTO_memcmp) |
| 08 crypto | Rust | `cd rust && cargo fetch && cargo run --release` | FIXED-THEN-RAN (added sha2/argon2 + Part A impl; sha256 vs argon2id ~95000x, Parts B & C run) |

## Blocked items — all seven cleared on 2026-08-19

The seven per-language items that were offline/tool-blocked in the first pass
all RAN in the 2026-08-19 unblock pass (details in the append section below).
The only thing still blocked is the shared compose lab, and not for a reason
Docker being up can fix.

## The shared compose lab — STILL NOT verifiable (missing source, not a dead daemon)

`07-security/lab/README.md` specifies a `docker compose` stack that **extends a
repo-wide `lab-harness/`** (FastAPI + Postgres + Toxiproxy + k6 + otel-lgtm) plus
security-only services (pgbouncer, redis, internal-admin, metadata, rebind-dns).
The Docker daemon is now **up**, so the original second blocker is gone — but the
first one is fatal on its own:

1. **`lab-harness/` does not exist on disk** — the compose command references
   `../../lab-harness/compose.yml`, which is absent (repo root has no such dir).
2. **None of the lab's own service source exists either** — `07-security/lab/`
   contains only `README.md`. There is no `lab/api/`, `lab/internal-admin/`,
   `lab/metadata/`, `lab/idp/`, `lab/rebind-dns/`, `lab/collector/`, no
   `compose.yml`, no `seed.py`, no `attack.sh`.

**What it would need:** the `lab-harness/` stack and the seven `lab/*` service
builds, plus `lab/compose.yml`, `lab/seed.py`, and `lab/attack.sh`, actually
written to disk. `docker compose … up` and `./attack.sh <topic> <scenario>`
cannot be run against files that do not exist — Docker being up does not create
them. The per-topic self-contained programs above remain the runnable stand-in;
the compose/attack.sh form is still the reader's exercise once the harness and
service sources are authored. **Note on the attack scripts:** because `attack.sh`
and the services it drives do not exist, there is no before-fix/after-fix attack
to run here at all — the "genuinely demonstrates then genuinely fails" check has
nothing to execute against and stays deferred with the lab.

## Findings from this pass

- **Coverage is honest.** Every topic ships the languages its
  `How each language actually gets there` section justifies (Topic 1: 4; Topic 3
  & 5: 3; Topic 4: 5, no C++; Topics 2, 6, 7, 8: all six). No topic was silently
  narrowed. `topicsIncomplete`: none.
- **Experiments test their stated claim.** Spot-audited the higher-risk ones:
  Topic 2's blind-extraction request count (1258) is a genuine per-character query
  loop (`requests += 1` around a real DB round-trip), deterministic given a fixed
  secret and charset — hence identical across languages, not a hardcoded constant.
  Topic 8's Part-B timing signal genuinely climbs with matching leading bytes for
  the naive compare and stays flat for the constant-time compare, in every runtime.
  Topic 5's `code-expiry` uses a logical clock (no real 61 s sleep), so no hang.
- **No fabricated numbers in README prose**; every `Predict, then record` table is
  blank (headers only).
- **No README `How to run` command needed correction** — all match the files.
- **No stray artifacts left**: two `rust/target/` dirs created by my `cargo run`
  verification (Topics 6, 8) were removed; the pip-installed `evilpkg` was
  uninstalled; the Node fixture was installed in a scratch dir, not the repo.

---

## Unblock pass — Docker daemon up (2026-08-19)

The Docker daemon came up and the network became reachable. The eight items the
first pass recorded as BLOCKED were re-run. Seven of them are per-language
programs that were blocked only on an offline crate/jar/npm fetch or a
host-absent build tool; all seven now RAN (one after a source fix). The eighth,
the shared compose lab, stays blocked because its source does not exist on disk
(see the section above) — Docker being up does not change that.

**Rule followed:** nothing was installed on the host. Gradle, Conan and cmake
were run inside throwaway (`--rm`) Docker containers; `pip install conan` ran
*inside* the container, not on the host.

### Cleared

- **02 sqli / Rust (sqlx)** — `cargo fetch && cargo run` against the host
  Postgres. Real output: tautology `-> 3 rows: ["alice","bob","carol"]`
  vulnerable / `0 rows` parameterized; UNION leaked
  `S3CR3T_KEY_abcdef0123456789abcd0`; boolean-blind recovered the 32-char key in
  **1258 requests** (263 ms). Matches Python/Go/C++/Java to the request — the
  1258 is a genuine per-character loop, not a constant.

- **02 sqli / Java (JDBC)** — driver jar fetched to `/tmp/pg.jar`; `java -cp
  /tmp/t2java:/tmp/pg.jar Sqli` against the host Postgres. Tautology `3 rows`
  vulnerable / `0 rows` parameterized; UNION `secret leaked:
  S3CR3T_KEY_abcdef0123456789abcd0`; blind extraction **1258 requests**
  (1284 ms). Same count as the other languages.

- **03 xss / Node+React** — `npm install` (added 5 packages) then
  `node xss_context.js`. Real output: text child **NO** (structural escape,
  `&lt;script&gt;…`), dangerouslySetInnerHTML **YES**, `javascript:` href
  **YES** (React only warns), attribute break-out **YES** → **3/4 payloads
  executed**. Demonstrates React's URL-context miss exactly as the topic claims.

- **04 jwt / Rust (jsonwebtoken)** — `cargo fetch && cargo run`. Real output:
  legit RS256 **ACCEPTED**; forged HS256 against `Validation[RS256]`
  **REJECTED (InvalidAlgorithm)**; revocation latency `plain 1500 ms` (= full
  remaining TTL) vs `denylist 0 ms` vs `opaque_introspect 0 ms`. The alg-confusion
  reject and the plain-vs-denylist latency gap are the topic's two claims.

- **07 supply / Java+Gradle** — run in a container:
  `docker run --rm -v "$PWD":/w -w /w gradle:8.10-jdk21 gradle help`.
  Under `> Configure project :`, **before any task**, it printed
  `[build.gradle.kts] configuration-time code executed -- this is arbitrary
  code`, then `BUILD SUCCESSFUL`. Proves the build script is arbitrary Kotlin
  that runs at configuration time.

- **07 supply / C++ Conan + vcpkg** — both run in throwaway containers:
  - Conan: `python:3.12-slim` + `pip install conan`, `conan profile detect`,
    `conan install .`. The recipe's top-level line fired —
    `[conanfile.py] imported and executed during `conan install` -- arbitrary
    Python, as you`. (The graph then errored `settings.compiler value not
    defined` because the slim image has no compiler in its detected profile;
    that is downstream of, and does not affect, the demonstrated point that the
    recipe is Python that executes on import.)
  - vcpkg portfile: `alpine` + `apk add cmake`, `cmake -P portfile.cmake`
    (a portfile is a CMake script). It printed
    `-- [portfile.cmake] executing during vcpkg install -- arbitrary CMake, as
    you`. Proves the portfile executes as ordinary CMake.

### Fixed, then ran

- **08 crypto / Rust — Part A** — was printing a skip note, so it never
  demonstrated the sha256-vs-argon2id cost gap. Fixed: added `sha2 = "0.10"` and
  `argon2 = "0.5"` to `rust/Cargo.toml` and implemented `part_a()` (sha256 loop
  for verify/sec, argon2id at the OWASP baseline m=19456/t=2/p=1, and the same
  crack-time model as the Python/Go versions). `cargo fetch && cargo run
  --release` now prints real numbers:
  `sha256 5,074,087 verify/sec` vs `argon2id(19MiB) 53.4 verify/sec`
  (**~95,000× slower**); crack-time `sha256 0.000020 s` vs `argon2id 1.9 s`.
  Part B still shows the timing signal genuinely climbing for the naive compare
  (`k=0:1.10 → k=31:13.18 ns/op`) and flat for the constant-time compare
  (`~3.7 ns/op` at every k); Part C unchanged (inproc workers=4 → effective 40
  vs configured 10; IP-keyed with 50 IPs reaches the password). All three parts
  now demonstrate their claims.

### Still blocked

- **The shared compose lab** (`07-security/lab/` + `lab-harness/`) — the harness
  directory and every `lab/*` service source (`compose.yml`, `seed.py`,
  `attack.sh`, `api/`, `internal-admin/`, `metadata/`, `idp/`, `rebind-dns/`,
  `collector/`) do not exist on disk; only `lab/README.md` is present. Needs
  those files authored before `docker compose … up` or `./attack.sh` can run.

### Teardown / cleanup

Build artifacts created for verification were removed: the `rust/target/` dirs
for Topics 2, 4, 8; `03-.../nodejs/node_modules` and `package-lock.json`;
`/tmp/pg.jar` and `/tmp/t2java`; and the ephemeral `sqli_lab` Postgres database.
The Gradle/Conan/cmake containers were `--rm` and left nothing on the host. The
one intentional source change kept on disk is Topic 8's Rust Part A
(`Cargo.toml` + `src/main.rs`, plus the regenerated `Cargo.lock`).
