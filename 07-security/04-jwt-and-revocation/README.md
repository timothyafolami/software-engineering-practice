# Layer 7 · Topic 4 — What a JWT actually is, and the revocation problem you cannot wish away

### The takeaway (read this first)

**The one idea:** a JWT is a **signed, not encrypted** blob — anyone holding
it can read every claim inside (it is just base64url JSON), and the
signature only proves it was issued by someone with the key and not
tampered with. The consequence that trips up real systems: a standard JWT
is **valid until it expires, and there is no built-in way to revoke it
early**, because verification is a local signature check that never phones
home. Statelessness *is* unrevokability; they are the same property, seen
from two sides.

**Why it matters in practice:** teams reach for JWTs to avoid a session
store, then discover they cannot log a user out, cannot kill a stolen token
(the one Topic 3 just exfiltrated), and cannot deprovision a fired
employee's access until their token expires. Authentication Failures is
**A07** in the OWASP Top 10:2025, and "we cannot revoke" is one of its most
common concrete forms. Knowing *when that is disqualifying* is the senior
judgement call.

**You'll know it landed when:** asked "how do you log someone out with
JWTs," you do not say "delete the token" (you cannot — it is on the client);
you explain the actual options and their costs, and you can state the one
question that decides whether stateless JWTs are appropriate for a given
system at all.

## The concept

A JWT is three base64url parts joined by dots: header, payload (the claims —
`sub`, `exp`, `iat`, `jti`, whatever you add), and signature. **Decode the
middle part and you read everything** — no key required — so never put a
secret in a JWT, and understand that `exp` is enforced only because the
verifier bothers to check it, not because anything expires on its own.

Verification is: recompute the signature over `header.payload` with the key,
compare, check `exp` (and `aud`, and `iss`, if you were careful). All local.
No database, no network. **That locality is the entire selling point** —
fast, horizontally scalable, no session lookup on the hot path — **and the
entire problem** — there is nothing to delete in order to revoke.

So revocation, when you need it, means giving back some statelessness. The
options, cheapest to most complete:

- **Short TTLs + refresh rotation.** Access tokens live 5–15 minutes; a
  long-lived, *single-use* refresh token is exchanged for a new access token
  and rotated, with **reuse detection** — presenting an already-used refresh
  token burns the entire token family. Revocation becomes "refuse to
  refresh," bounded by the access-token TTL. This is the current default
  answer (RFC 9700 / BCP 240, the OAuth 2.0 Security Best Current Practice:
  public-client refresh tokens must be sender-constrained or rotated on
  every use). It does not make access tokens revocable — it makes them
  expire fast enough that you stop caring, which is a different and honest
  claim.
- **A denylist / token version.** Keep a small store (Redis) of revoked
  `jti`s, or a per-user `token_version` compared against a claim; check it
  on each request. This works, and you have re-added the session lookup you
  used JWTs to avoid — so ask honestly whether opaque session tokens are now
  the simpler system.
- **Opaque tokens + introspection (RFC 7662).** The API asks the auth server
  "is this token still good," giving a real kill switch at the cost of a
  network hop on the hot path. The honest choice when live revocation is a
  hard requirement.
- **Sender-constrained tokens (DPoP, RFC 9449, or mTLS).** Binds the token
  to a key the client holds, so a stolen token is useless without the key.
  This does not give you revocation; it gives you theft-resistance — a
  different axis, and worth naming precisely so you do not offer it as an
  answer to the wrong question.

**The decision rule:** if you need to revoke access faster than your token
TTL, a plain JWT is the wrong tool. Either shorten the TTL until the window
is acceptable, or move to server-side state and accept the lookup. Choosing
JWTs *and* requiring instant revocation is choosing a contradiction, and
the experiment below is designed to make you feel the size of it in
milliseconds rather than argue about it in the abstract.

### The `alg` family of bugs, which are all one bug

The header carries `alg`, and the header is attacker-supplied. Three
classic failures follow from a verifier that trusts it:

- **`alg: none`** — the spec defines an unsecured JWT with an empty
  signature. A verifier that honours the header accepts anything.
- **RS256 → HS256 confusion** — the attacker takes your *public* key, which
  is public, and uses it as an HMAC secret to sign a forged token with
  `alg: HS256`. A verifier that reads the algorithm from the token and
  looks up "the key" will happily HMAC-verify against the public key and
  succeed.
- **`kid` injection** — a `kid` header used to look up a key file or a
  database row, unsanitised.

All three have the same fix and it is one line: **pin the accepted algorithm
list at the call site** (`algorithms=["RS256"]`), and never let the token
choose how it is checked. Note the shape — attacker-controlled input
selecting which code runs — and notice you have now seen it in three
consecutive topics.

## How each language actually gets there

Five, not six. C++ is omitted because it has no canonical JWT library and
would only demonstrate a fourth wrapper around the same HMAC call; the
variable in this topic is **library defaults**, not runtime behaviour.

**Python (`PyJWT`, your stack).** `jwt.decode(token, key, algorithms=[...])`
made `algorithms` **required** years ago precisely because of the confusion
attack — an API change driven by a CVE class, which is itself worth
noticing. `jwt.decode(..., options={"verify_signature": False})` is the
legitimate way to read claims without a key, and `jwt.get_unverified_header`
is how tools inspect `alg`; both are also how people accidentally ship a
verifier that verifies nothing. For revocation, the idiomatic FastAPI
pattern is a dependency that checks Redis — at which point you have a
session store and should re-ask the question.

**Node (`jsonwebtoken`).** `jwt.verify(token, secret)` without an
`algorithms` option historically accepted whatever the header said; the
library now requires you to be explicit in the common paths, but the older
shape is everywhere in existing code and in copied answers. Node's specific
hazard is that `secret` is a `string | Buffer` and an RSA public key is a
string too, so the confusion attack is a type-check-passing mistake.

**Go (`golang-jwt/jwt`).** Verification takes a `Keyfunc` that receives the
parsed token, and the *documented* correct implementation is to assert
`token.Method.(*jwt.SigningMethodRSA)` inside it before returning a key.
That is unusually honest API design — it hands you the attacker-controlled
algorithm and makes you write the check — and it means Go code that skips
the assertion is visibly missing a line rather than relying on an absent
default.

**Java (Nimbus JOSE+JWT / `java-jwt`).** The most explicit of the five:
`JWSVerifier` instances are algorithm-specific by construction, so a
`RSASSAVerifier` cannot be tricked into HMAC. Java is also where you meet
JWKS handling in its most complete form (key rotation, `kid` lookup,
caching), which matters for Topic 5 — and where a cached JWKS with no
refresh becomes an availability bug the day the IdP rotates.

**Rust (`jsonwebtoken`).** `Validation` is a struct with an `algorithms`
field that has no meaningful "accept anything" value, and `DecodingKey` is
typed by key kind — so the RS256/HS256 confusion is a type error rather
than a runtime acceptance. Same pattern as Topic 2's `sqlx`: the compiler is
enlisted, and the vulnerable version has to be written on purpose.

## The experiment

Uses the shared [`lab/`](../lab/README.md) stack: `api` issues and verifies
tokens, `redis` holds the denylist, and `JWT_STRATEGY` selects the design.

**Part A — it is not encrypted.** Take a token and read the claims with
nothing but a shell. This takes ten seconds and settles the argument
permanently.

**Part B — the revocation gap, measured.** For each strategy, log in, start
polling `GET /me` with the *already-issued* access token every 50 ms, then
`POST /logout`, and record the wall-clock milliseconds from the `/logout`
200 to the first `401`. That number is **revocation latency**, and it is the
number this topic exists to produce:

| Strategy | what "logout" does |
|---|---|
| `plain` (`JWT_TTL_SECONDS=86400`) | invalidates a server-side session that `/me` does not consult |
| `short_ttl_rotate` (`JWT_TTL_SECONDS=300`) | refuses the next refresh |
| `denylist` | writes the `jti` to Redis, checked on every request |
| `opaque_introspect` | marks the token dead at the issuer; `api` introspects per request |

For `plain` and `short_ttl_rotate`, cap the poll at the TTL — the answer is
"the full remaining TTL," and you want that as a measured number in
milliseconds sitting next to the others, not as a shrug.

**Part C — what revocation costs.** Run `/me` at a constant 200 rps for 60
seconds under each strategy and record **p50 and p99 latency**, plus Redis
ops per request. This is the honest cost side of the trade and it is small
but not zero; the point is to have your own number for it rather than an
opinion.

**Part D (optional) — `alg` confusion.** Configure `JWT_ACCEPT_ALGS` to
include both `RS256` and `HS256`, forge a token signed with the public key
as an HMAC secret, and confirm it verifies. Then pin the list and watch it
stop.

### How you'd know the fix is fake

The denylist that is cached per worker. A `@lru_cache` or a module-level
dict in front of the Redis lookup turns instant revocation into
"instant on one worker, up to a TTL on the others," and your single-worker
laptop test will never show it. Run Part B with `WORKERS=4` and poll through
the load balancer: if revocation latency is bimodal — some polls flip
immediately, some do not — you have found a per-process cache, and the fix
looked perfect until you added a second process.

## How to run

Each program is self-contained (it generates its own RSA keypair) and runs
Part A (a JWT is signed, not encrypted), the alg-confusion attack (README
Part D — a naive verifier accepts a forged HS256 token signed with the RSA
public key; a pinned verifier rejects it), and a revocation-latency
simulation for the four strategies.

```
python3 python/jwt_demo.py                                    # PyJWT
node   nodejs/jwt_demo.js                                     # node:crypto (jsonwebtoken not cached)
cd golang && GOFLAGS=-mod=mod GOPROXY=off go run jwt_demo.go && cd ..   # golang-jwt/v5
cd java && javac JwtDemo.java -d /tmp/t4java && \
           java -cp /tmp/t4java JwtDemo && cd ..              # JDK crypto (Nimbus not cached)
```

The Rust version is idiomatic but blocked here — the `jsonwebtoken` crate is
not in the cargo cache. One-time online fetch, then run:

```
cd rust && cargo fetch && cargo run && cd ..
```

The revocation-latency numbers are produced by a logical-clock simulation, so
they need no Redis. Part B (`WORKERS=4` per-worker-cache fake-fix check) and
Part C (200 rps p50/p99 cost) genuinely need the compose `api` + `redis`; they
belong to `lab/` once Docker is up. The Python `jwt_demo.py` output also
records that modern PyJWT closes alg-confusion two ways (required
`algorithms=` AND refusing an asymmetric key as an HMAC secret).

## Predict, then record

1. After `/logout`, does the old access token still authenticate, and for
   how many milliseconds? Predict a number for each of the four strategies.
   Two of them you can derive exactly from a config value; say which, and
   what that tells you.
2. Reading the payload with `base64 -d` — do you need the signing key? What
   does that imply about putting a user's role, email, or internal user id
   in a JWT?
3. With the Redis denylist added, what did you give up that was the reason
   to use JWTs in the first place, and how many milliseconds of p99 did it
   cost you? Predict the p99 delta before measuring.

| Strategy | revocation latency p50, ms | revocation latency p95, ms | `/me` p50 ms @200 rps | `/me` p99 ms @200 rps | Redis/network ops per request |
|---|---|---|---|---|---|
| `plain`, 24h TTL |  |  |  |  |  |
| `short_ttl_rotate`, 300s |  |  |  |  |  |
| `denylist` (Redis `jti`) |  |  |  |  |  |
| `opaque_introspect` |  |  |  |  |  |

| Fake-fix check | value |
|---|---|
| `denylist` revocation latency p50, `WORKERS=4`, ms |  |
| `denylist` revocation latency p95, `WORKERS=4`, ms |  |
| spread between the two (p95 − p50), ms |  |

**What would mean the experiment is broken, not the prediction:** if the old
token *stops* working after `/logout` in the `plain` case, your `/me`
endpoint is doing a server-side session lookup somewhere — you are not
testing stateless verification, so go find the hidden state before believing
any row. If revocation latency for `plain` comes back as something other
than "the remaining TTL," check that your poller is actually re-sending the
*old* token and not silently refreshing. If the denylist adds no measurable
p99 at all, confirm Redis is on the request path: an in-process fallback
client that silently no-ops on connection failure will produce exactly that
result, and it also means nothing is being revoked. If p99 is enormous for
every strategy including `plain`, your load generator is closed-loop and you
are measuring your own client.

## Answer before moving on

1. State the one question whose answer decides whether stateless JWTs are
   appropriate for a given endpoint. It is a comparison between two
   durations — name both.
2. A colleague stores the user's `is_admin` flag in the JWT to avoid a DB
   lookup. Two distinct things are wrong with this. Name both — one about
   revocation, one about what "signed, not encrypted" does and does not buy.
3. Refresh token rotation with reuse detection: walk through, step by step,
   what happens when an attacker steals a refresh token and the real user
   next tries to refresh. Why does the *legitimate* user get logged out, and
   why is that the correct behaviour?
4. Your measured `denylist` p99 delta is some number of milliseconds. At
   what request rate, and what value of that delta, would you change your
   mind and move to `opaque_introspect` instead — and what would you have to
   measure to know you had reached it?

## Next up

[Topic 5 — OAuth2, OIDC, and PKCE end to end](../05-oauth2-oidc-and-pkce/README.md).
This topic assumed the token already existed; the next one is about how it
gets issued, and about the single hash that stops an intercepted
authorization code from being worth anything.
