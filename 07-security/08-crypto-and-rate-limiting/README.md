# Layer 7 · Topic 8 — Crypto hygiene and rate limiting: timing, and abuse as a security control

### The takeaway (read this first)

**The one idea, part A (crypto):** never invent your own — use vetted
primitives (**argon2id** or **bcrypt** for passwords), compare secrets in
**constant time**, and understand that a **nonce** is a number used *once*
per key, whose reuse breaks the primitive rather than merely weakening it.
**Part B (rate limiting):** rate limiting is a *security* control, not a
performance one — it is what stands between your login endpoint and a
credential-stuffing botnet working through a list of leaked passwords.

**Why it matters in practice:** password storage sits under A04
(Cryptographic Failures) in the OWASP Top 10:2025, and the difference
between argon2id and a raw SHA-256 is the difference between a leaked
database that is embarrassing and one that is over. A login endpoint with no
limit is a standing invitation to credential stuffing — the most common way
accounts are actually taken over at scale, because it requires no
vulnerability in your code at all.

**You'll know it landed when:** you reach for argon2id without thinking, you
use a constant-time comparison for any secret equality check and can explain
the timing attack it prevents, and you treat "how many times per minute can
an anonymous caller hit this" as a design-time question with a number for an
answer.

## The concept

### Password hashing

You are not storing passwords, you are storing **verifiers deliberately
expensive to compute**. A fast hash (SHA-256, MD5) is wrong precisely
*because* it is fast: the same property that makes it a good checksum — high
throughput per byte — is what lets an attacker with your dump try enormous
numbers of candidates per second on commodity GPUs. argon2id and bcrypt are
slow and (for argon2id) **memory-hard** by design, with tunable cost so you
can keep them expensive as hardware improves. Memory-hardness is the part
worth understanding: it attacks the *attacker's* hardware advantage
specifically, because GPUs have many cores and comparatively little memory
per core.

Parameters are meant to be tuned, not defaulted. OWASP's
[Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)
gives argon2id configurations starting at **m=19 MiB, t=2, p=1** for
constrained environments and rising from there for standard web
applications; the lab's default is that baseline, and Part A of the
experiment measures what raising it costs *you*, on *your* machine, which is
the only version of that number worth having. Salting is per-password and
the libraries do it for you, storing the salt inside the encoded hash string
— if you are handling salts by hand, you are using the wrong API.

### Constant-time comparison

`if provided == real` returns as soon as it finds the first differing byte,
so it takes measurably longer the more leading bytes match. An attacker who
can measure response time recovers the secret byte by byte: guess byte 1
across 256 values, keep the slowest, move to byte 2 — turning an
exponential search into a linear one. A constant-time comparison examines
every byte regardless (typically accumulating differences with XOR and OR,
then testing the accumulator once), so the timing carries no information
about *where* the mismatch was.

The subtlety that makes this a six-language topic: **constant-time code is a
property of the machine instructions, not of your source**, and every layer
between the two can undo it. A compiler may replace your careful loop with a
short-circuiting `memcmp`. A JIT may specialise it after profiling. A
branch predictor may leak. This is why every language's answer is a
*library function whose implementation is defended*, not a technique you
hand-roll.

### Nonces and IVs

A nonce is a value used exactly once per key. Reuse breaks things
concretely rather than gradually: repeat an IV in AES-CTR or AES-GCM and the
keystream repeats, so XORing the two ciphertexts yields the XOR of the two
plaintexts — and for GCM, nonce reuse also enables forgery of further
messages. "Never roll your own crypto" is mostly "you will get the nonce
discipline wrong." Use an AEAD library that manages nonces for you, or a
misuse-resistant construction.

### Rate limiting as a security control

The point is not to keep servers from melting; it is to make **online
guessing infeasible**. Four design questions, each with a real answer:

- **Limit by what?** IP alone is evadable by any botnet or residential proxy
  pool. Combine per-IP with per-account (so one target cannot be attacked
  from many sources) and per-credential-pair.
- **What budget?** Login: single digits per minute per account. Password
  reset and MFA verification: tighter. The budget should come from "what
  does a legitimate human need," which is a much smaller number than people
  assume.
- **Which algorithm?** Token bucket tolerates bursts and is the usual right
  answer; sliding window is smoother and more expensive to store.
- **Where does the state live?** In a shared store. **An in-process counter
  multiplies your configured limit by the number of workers**, silently, and
  the multiplier changes when you scale — a limit that gets weaker under
  load, which is exactly backwards. This is the measurable failure in Part C.

Return `429`, emit `RateLimit-*` headers so honest clients back off (the
IETF `RateLimit` header fields draft standardises the names that were
already de-facto), and **do not leak whether the account exists** in the
process — a rate limiter that only triggers for real usernames is an account
enumeration oracle.

## How each language actually gets there

All six. **The runtime is the subject**: whether a comparison stays
constant-time depends on the compiler, the JIT and the memory model, and
these six sit at six different points on that spectrum.

**Python.** `hmac.compare_digest` is implemented in C and is the correct
call. Python's interpretive overhead per byte is so large that it *masks*
the timing signal — the same masking effect Layer 1 Topic 1 found with cache
misses, appearing again in a security context. A naive `==` in Python is
genuinely harder to exploit remotely than the same bug in C, and that is a
fact about noise, not about safety.

**Node.** `crypto.timingSafeEqual` — and it **throws if the two buffers
differ in length**, which forces you to think about the length leak that
every constant-time comparison has and most people forget: comparing hashes
rather than raw secrets removes it, because hashes are fixed length.

**Go.** `crypto/subtle.ConstantTimeCompare`, plus the rest of the `subtle`
package (`ConstantTimeSelect`, `ConstantTimeByteEq`) — a standard-library
acknowledgement that this is a category of operation, not a single function.
`golang.org/x/crypto/argon2` for hashing, `golang.org/x/time/rate` for the
token bucket (in-process, so the distributed-state problem is yours).

**Rust.** The `subtle` crate's `ConstantTimeEq` returns a `Choice`, not a
`bool` — a deliberate type-level obstacle to writing `if secrets_match { }`,
because a branch on the result reintroduces the timing difference the
comparison just removed. That is the sharpest example in this lab of a type
system encoding a *side-channel* property rather than a correctness one.
Rust also needs `black_box` to stop LLVM optimising benchmark work away,
which is the same hazard in benchmark form.

**C++.** No standard constant-time comparison exists. `memcmp` short-circuits
and is heavily vectorised; a hand-rolled XOR-accumulate loop is correct
*until* the optimiser proves it can exit early or vectorises it into
something with data-dependent timing. The real answer is to call a crypto
library's own comparison (OpenSSL's `CRYPTO_memcmp`, libsodium's
`sodium_memcmp`). This is the language where "constant time is a property of
the emitted instructions" stops being an abstraction: compile the same
source at `-O0` and `-O2` and read the disassembly.

**Java.** `MessageDigest.isEqual` is documented as constant-time for equal
lengths. The JVM twist is unique in this set: HotSpot profiles and
recompiles hot methods, so a comparison's timing behaviour can *change*
after a few thousand invocations. A timing experiment on the JVM that skips
warm-up measures the interpreter, not the deployed system — the same
warm-up discipline Layer 1 needed for its locality benchmark, now with a
security consequence attached.

## The experiment

Uses the shared [`lab/`](../lab/README.md) stack: `api`, `redis`, and k6 in
open-model mode.

**Part A — hash cost.** Time `PASSWORD_HASH=sha256`, `bcrypt` and
`argon2id` (at the OWASP baseline, then at 2× and 4× the memory parameter).
Measure **verifications per second** in-process, then derive on the page:
*given V verifications/sec, an attacker with an N×-faster rig, and a
K-candidate password list, the expected time to a first crack is
K / (V × N) seconds.* Fill in your own V; pick N and K explicitly and write
them down, because the whole point is that the conclusion follows from
numbers you chose and can defend.

**Part B — timing signal.** `/verify-key` compares a 32-byte API key with
`COMPARE_MODE=naive_eq` or `constant_time`. Send a large sample of requests
per bucket, where a bucket is "number of matching leading bytes," and
compare **latency distributions** — not single requests. Record the
difference between the p50 of the 0-matching-bytes bucket and the p50 of the
16-matching-bytes bucket, in nanoseconds, both **on loopback** and **across
the compose network**. The comparison between those two environments is the
finding: it tells you when this attack is real and when it is a puzzle.

**Part C — rate limiting.** A credential-stuffing simulation: one target
account, a 1,000-entry password list with the correct password planted at
position 500, run at a constant arrival rate. Measure **attempts to first
success** (median of 5 runs), **successful attempts per minute at steady
state**, and the **429 rate**, under:

| Mode | configuration |
|---|---|
| `off` | no limit |
| `redis_token_bucket` | `RATELIMIT_PER_MIN=10`, shared state |
| `inproc`, `WORKERS=1` | in-process counter, one worker |
| `inproc`, `WORKERS=4` | in-process counter, four workers |

The last two rows exist to produce one number: **effective limit ÷
configured limit**. Derive what you expect it to be from the mechanism
before you run it.

### How you'd know the fix is fake

Three, all common. **A limiter keyed only on IP** passes this experiment
completely and stops nothing real — re-run Part C with the attempts spread
over 50 source addresses and watch attempts-to-first-success return to the
unlimited value. **A limiter that returns 429 but still performs the
password check** leaves the expensive verify on the attacker's critical path
in the wrong direction: it is now a CPU-exhaustion vector, since argon2id at
46 MiB is a *large* amount of work to hand an anonymous caller. **A limiter
that only counts failures** lets an attacker with one valid credential
probe indefinitely.

## How to run

Each program runs all three parts and prints measured numbers (nothing is
invented): Part A verifications/sec for a fast hash vs a slow KDF plus the
crack-time model, Part B the timing signal as average ns/op per
matching-leading-bytes bucket (naive short-circuit climbs; the constant-time
compare stays flat), and Part C the credential-stuffing effective limit and
the two fake fixes.

```
python3 python/crypto_ratelimit.py                              # sha256 vs argon2id
cd golang && GOFLAGS=-mod=mod GOPROXY=off go run crypto_ratelimit.go && cd ..   # argon2id (x/crypto)
node   nodejs/crypto_ratelimit.js                               # sha256 vs scrypt (built-in KDF)
cd java && javac CryptoRateLimit.java -d /tmp/t8java && \
           java -cp /tmp/t8java CryptoRateLimit && cd ..        # pbkdf2; warms HotSpot first
SSL=/opt/homebrew/opt/openssl@3; g++ -O2 -std=c++17 -I$SSL/include -L$SSL/lib \
  -o /tmp/cpp_crl cpp/crypto_ratelimit.cpp -lcrypto && /tmp/cpp_crl   # CRYPTO_memcmp
cd rust && cargo run --release && cd ..                         # Part B (Choice/black_box) + Part C
```

Notes on what each runtime can show offline: Python uses **argon2id** (the
OWASP baseline); Go uses **argon2id** via `x/crypto`; Node uses the built-in
**scrypt** and C++/Java use **PBKDF2** because argon2/bcrypt are native
libraries not present here; Rust's Part A needs a hashing crate (`sha2`,
`argon2`) not in the cargo cache, so it prints Parts B and C and points at the
others for the hash numbers — its emphasis is Part B anyway (`subtle`'s `Choice`
type and `black_box`). The full sweep at real network distance (loopback vs
compose, the p50-per-bucket in ns across a socket) belongs to `lab/` + k6 once
Docker is up; the constant-time property and the rate-limit arithmetic are
settled by the programs above.

## Predict, then record

1. Predict verifications/sec for SHA-256 and for argon2id at the baseline
   parameters, as an order of magnitude, and then predict the *ratio*. The
   ratio is the prediction that matters; the absolute numbers are your
   machine's.
2. In Part B, will you see the timing signal over the compose network, or
   only on loopback? Predict the p50 delta in nanoseconds for each. What
   does your answer imply about when timing attacks are the real threat?
3. With `inproc` and 4 workers, what is the effective limit as a multiple of
   the configured one? Derive it, then measure it.

| Hash | verifications/sec (1 core) | ms per verify (p50) | derived: seconds to try your K-candidate list at N× |
|---|---|---|---|
| SHA-256 |  |  |  |
| bcrypt (default cost) |  |  |  |
| argon2id m=19 MiB t=2 p=1 |  |  |  |
| argon2id m=39 MiB t=2 p=1 |  |  |  |
| argon2id m=78 MiB t=2 p=1 |  |  |  |

State your K and N above the table before filling it in.

| Timing (p50 of 16-matching-bytes bucket − p50 of 0-matching bucket) | loopback, ns | compose network, ns |
|---|---|---|
| `naive_eq` |  |  |
| `constant_time` |  |  |
| samples per bucket used |  |  |

| Rate limiting | attempts to first success (median of 5) | successful attempts/min at steady state | 429 rate | effective ÷ configured limit |
|---|---|---|---|---|
| `off` |  |  |  | — |
| `redis_token_bucket`, 10/min |  |  |  |  |
| `inproc`, 1 worker |  |  |  |  |
| `inproc`, 4 workers |  |  |  |  |
| `redis_token_bucket`, 50 source IPs |  |  |  |  |

**What would mean the experiment is broken, not the prediction:** if
argon2id verifies as fast as SHA-256, your cost parameters are near-zero —
check `ARGON2_M_KIB`, `ARGON2_T`, `ARGON2_P` are actually applied and not
silently defaulted. If you see *no* timing signal even on loopback with
`naive_eq`, either your comparison is not short-circuiting (some
implementations compare hashes, not raw values), or the difference is below
your timer's resolution: increase the secret length and the sample count,
and compare distributions rather than means. If the timing signal is
*identical* for `constant_time` and `naive_eq`, confirm the mode flag is
reaching the handler — a `/admin/config` value read at import time will not
change behaviour. If the Redis-limited endpoint shows no improvement over
unlimited, confirm the limiter shares state in Redis and is not silently
falling back to per-process memory on a connection error — which is the
same fake-fix shape as Topic 4's per-worker denylist cache. If
attempts-to-first-success is exactly 500 in every row, your password list is
being tried in order and you have measured your list, not the limiter:
shuffle it per run and report the median.

## Answer before moving on

1. Why is SHA-256 the *wrong* choice for password storage for the exact same
   reason it is the *right* choice for a file checksum? Name the single
   property, and note that the requirement inverted while the property did
   not.
2. Explain the timing attack on `==` precisely enough that someone could
   implement it: what the attacker measures, how they use it to recover byte
   N, and why a constant-time comparison removes the signal. Then say what
   your Part B numbers imply about doing it over the internet.
3. IP-based rate limiting is trivially evaded by a botnet — your 50-source-IP
   row measured exactly that. So why implement it at all, and name at least
   two other layers you combine it with to actually stop credential
   stuffing.
4. Your rate limiter is correct, shared, and keyed on both IP and account.
   Describe the denial-of-service an attacker can now perform *against a
   specific user* using nothing but your limiter, and the standard mitigation.

## Next up

That is the layer. Return to the [index](../README.md) for the
threat-modelling reflex that ties the eight topics together — the point at
which you stop running other people's experiments and start running STRIDE
on your own service.
