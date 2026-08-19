# Layer 7 · Topic 5 — OAuth2, OIDC, and PKCE end to end

### The takeaway (read this first)

**The one idea:** OAuth2 is a **delegation** protocol, not a login protocol
— it answers "may this application act on that user's behalf," and OIDC is
the thin layer on top that makes it answer "who is this user" as well. The
authorization code flow's whole security rests on one property: the code
travels through the **front channel** (the browser, the address bar, the
referrer header, the OS URL handler, your access logs) and is therefore
*not confidential*, so it is worthless on its own. **PKCE is what makes it
worthless** — the code is only redeemable by whoever can prove they started
the flow.

**Why it matters in practice:** "log in with Google" is four redirects and
about six parameters, and every parameter is there because something broke
without it. Teams that treat OAuth as a library call ship the version with
`state` unchecked (login CSRF), or `nonce` unchecked (ID token replay), or a
prefix-matched `redirect_uri` (code exfiltration through an open redirect),
or the ID token used as an API bearer token (audience confusion). None of
these are exotic; all of them pass a manual "I logged in and it worked"
test.

**You'll know it landed when:** you can draw the four redirects from memory,
say which parameter each of `state`, `nonce`, `code_challenge` and
`redirect_uri` defends and against what, and explain — in terms of a hash
preimage — why an attacker holding a valid authorization code cannot use it.

## The concept

The authorization code flow with PKCE, as eight steps. Read the `code_*`
parameters as one mechanism spread across steps 1, 2 and 6.

```
1. client: verifier = random(43..128 chars)
           challenge = BASE64URL(SHA256(verifier))
2. browser → AS   /authorize?response_type=code&client_id=...
                  &redirect_uri=https://app/cb&scope=openid+profile
                  &state=<random>&nonce=<random>
                  &code_challenge=<challenge>&code_challenge_method=S256
3. user authenticates at the AS, consents
4. AS → browser   302 https://app/cb?code=<code>&state=<state>
5. client checks the returned state against the one it stored for THIS browser session
6. client → AS    POST /token   grant_type=authorization_code
                  &code=<code>&redirect_uri=https://app/cb
                  &client_id=...&code_verifier=<verifier>      ← back channel
7. AS recomputes SHA256(verifier), compares to the stored challenge.
   Mismatch or missing → reject, and burn the code.
8. AS → client    access_token, refresh_token, id_token (OIDC)
   client verifies id_token: signature via JWKS, iss, aud == client_id,
   exp, and nonce == the one it sent in step 2
```

**Why each parameter exists**, which is the only way to remember them:

- **`code` is short-lived and single-use** because it crosses the front
  channel. RFC 9700 requires that a *reused* code invalidate the tokens
  already issued from it — reuse is evidence of interception, so the
  correct response is to kill the grant, not just to refuse the second
  exchange.
- **`code_verifier` / `code_challenge` (PKCE, RFC 7636).** The challenge is
  a SHA-256 hash, so an attacker who sees the `/authorize` request learns
  nothing usable, and an attacker who steals the `code` from the redirect
  cannot produce a verifier for it — that would require inverting SHA-256.
  Originally designed for mobile apps, where another app could register the
  same custom URL scheme and receive the redirect; OAuth 2.1 requires it for
  **all** clients, confidential ones included, because "the code leaked" has
  many more causes than a hostile mobile app.
- **`code_challenge_method`.** `S256` hashes; `plain` sends the verifier
  itself and is a no-op. An AS that accepts `plain` for a client that
  registered `S256` permits a **downgrade**: the attacker re-runs the
  authorize step with `plain` and a challenge of their own choosing.
- **`state`.** Binds the callback to the browser session that started the
  flow. Without it, an attacker completes a flow with *their* account, hands
  the victim the callback URL, and the victim's browser silently logs into
  the attacker's account — login CSRF, after which everything the victim
  does lands in the attacker's account.
- **`nonce` (OIDC).** Binds the *ID token* to this authorization request, so
  an ID token captured elsewhere cannot be replayed into your login. `state`
  protects the redirect; `nonce` protects the token. They are not
  substitutes and both are required.
- **`redirect_uri` exact matching.** Prefix or wildcard matching plus any
  open redirect on the registered host lets the attacker have the code
  delivered to themselves. The AS must compare the full string.

The two flows OAuth 2.1 removes are worth knowing as history that explains
the present: **implicit** (`response_type=token`, tokens returned in the URL
fragment — front-channel tokens, unfixable) and **resource owner password
credentials** (the app collects the user's IdP password, defeating the entire
point of delegation). If you see either in a codebase, you are looking at
pre-2019 advice.

**The one-line rule for OIDC:** the `access_token` is for calling APIs and is
opaque to you; the `id_token` is for your login and must never be sent to an
API as a bearer credential. Mixing them is audience confusion, and it is the
most common OIDC bug that is not a missing check.

## How each language actually gets there

Three, not six. **The protocol is HTTP redirects and one SHA-256** — it
behaves identically in every runtime — so the only language-side variable is
how much of the verification the library does for you and how much it leaves
in your callback handler.

**Python (`authlib`, your stack).** `authorize_redirect` generates and
stores `state`, `nonce` and the PKCE verifier in the session for you, and
`authorize_access_token` verifies the ID token including `nonce`. The trap
is what the framework glue does with the session: build the flow yourself
with `requests` and a hand-written callback — the shape most tutorials
show — and `state` becomes a variable you compare only if you remember to,
`nonce` is usually skipped entirely, and PKCE is absent because it needs
storage between two requests.

**Node (`openid-client`).** Discovery-first: it reads
`/.well-known/openid-configuration`, and its `callbackParams` / `callback`
API takes the expected `state` and `nonce` as *arguments*, so omitting them
is a visibly empty parameter rather than a missing line. That is a small but
real API-design lesson — a check you must pass a value to is harder to skip
than a check you must call.

**Go (`golang.org/x/oauth2` + `coreos/go-oidc`).** The most instructive,
because it is the most manual: `oauth2.Config.AuthCodeURL(state, ...)` takes
`state` as a required positional argument, PKCE goes in via
`oauth2.S256ChallengeOption`, and ID token verification is a separate
`oidc.Verifier` you construct yourself with the expected `ClientID`. Nothing
is implicit, which means nothing is silently wrong — but every check is
yours to write, and a Go OAuth handler is where you most often see `state`
generated and then never compared.

## The experiment

Uses the shared [`lab/`](../lab/README.md) stack: `idp` on `:8008` is a
minimal authorization server (`/authorize`, `/token`, `/introspect`, JWKS,
discovery document), `api` on `:8007` is the client, `redis` stores issued
codes with their challenges.

The scripted flow runs steps 1–8 with `curl`, capturing the redirect rather
than following it — which is exactly the attacker's position. **The code is
"intercepted" from the `Location` header**, the same place it would appear in
a referrer, a proxy log, or a mobile URL-scheme hijack; nothing about the
interception is simulated.

Then, holding that code, try to redeem it:

| Scenario | what the attacker sends to `/token` | `idp` config |
|---|---|---|
| `replay-no-verifier` | code, no `code_verifier` | `PKCE_MODE=required` |
| `replay-wrong-verifier` | code + a random 43-char verifier | `PKCE_MODE=required` |
| `replay-no-pkce` | code, no verifier | `PKCE_MODE=off` |
| `downgrade-plain` | re-run `/authorize` with `code_challenge_method=plain` and the attacker's own challenge, then redeem | `PKCE_MODE=optional` |
| `code-reuse` | legitimate exchange, then the *same* code again | `CODE_SINGLE_USE=true` |
| `code-expiry` | wait past `CODE_TTL_SECONDS`, then exchange | `CODE_TTL_SECONDS=60` |
| `redirect-prefix` | register `https://app/cb`, redeem with `https://app/cb.attacker.test` | `REDIRECT_URI_MATCH=prefix` vs `exact` |
| `no-state` | deliver a code from the attacker's own flow to the victim's callback | client-side check disabled |

The measurements are counts and milliseconds, not opinions: **tokens issued
(0 or 1)**, **HTTP status**, **requests to first success capped at 1,000**,
and for `code-reuse`, **whether the access token issued by the first,
legitimate exchange still works afterwards** — that last one is the
RFC 9700 requirement and the most commonly unimplemented line in a homegrown
authorization server.

### How you'd know the fix is fake

**PKCE that is sent but not verified.** The client dutifully generates a
verifier and posts it; the AS stores the challenge and never compares. Every
request looks textbook-correct on the wire, `replay-no-verifier` succeeds,
and no log line anywhere says anything is wrong. The only test that
distinguishes a verifying AS from a decorative one is sending a *wrong*
verifier and requiring a rejection — which is why `replay-wrong-verifier` is
in the table above `replay-no-verifier`, not below it. The same shape
applies to `state`: generating it proves nothing, comparing it is the
control.

## How to run

Each program models the `idp` in-process (the protocol is redirects and one
SHA-256, so no network is needed) and runs every attack scenario, printing
tokens issued (0 or 1) per scenario. `happy-path` issues 1; `replay-no-pkce`,
`downgrade-plain` and `redirect-prefix` issue 1 (the bugs); everything else
issues 0.

```
python3 python/pkce_flow.py
node   nodejs/pkce_flow.js
cd golang && go run pkce_flow.go && cd ..
```

The compose form (`./attack.sh oauth <scenario>` driving the real `idp` on
`:8008` with curl, printing the `/authorize` URL, intercepted `Location`, and
exact `/token` body) belongs to `lab/` once Docker is up. These programs
produce the same tokens-issued verdicts; the point they enforce — read the
`replay-wrong-verifier` row, not just `replay-no-verifier` — is identical.

## Predict, then record

1. `replay-no-verifier` under `PKCE_MODE=required`: what status does `/token`
   return, and — separately — what should happen to the *code* as a side
   effect? Predict both; the second one is where homegrown servers differ
   from correct ones.
2. `downgrade-plain`: does an AS that "supports PKCE" stop this? State the
   condition under which it does, in terms of what the AS remembered about
   the client.
3. `code-reuse`: after the second exchange is refused, is the access token
   from the *first* exchange still valid? Predict yes or no, then say which
   answer RFC 9700 requires and why the requirement is about interception
   rather than about tidiness.

| Scenario | `/token` status | tokens issued (0/1) | requests to first success (cap 1,000) | first-exchange access token still valid after? |
|---|---|---|---|---|
| `happy-path` |  |  | — |  |
| `replay-no-verifier` |  |  |  | — |
| `replay-wrong-verifier` |  |  |  | — |
| `replay-no-pkce` |  |  |  | — |
| `downgrade-plain` |  |  |  | — |
| `code-reuse` |  |  | — |  |
| `code-expiry` |  |  | — | — |
| `redirect-prefix` (prefix / exact) |  |  |  | — |
| `no-state` |  |  |  | — |

| Timing | value |
|---|---|
| seconds from code issue to `/token` in `happy-path` |  |
| `CODE_TTL_SECONDS` value at which `code-expiry` starts failing |  |
| ms added to login by the `/token` round trip (p50) |  |

**What would mean the experiment is broken, not the prediction:** if
`replay-no-verifier` *succeeds* while `PKCE_MODE=required`, check whether
`idp` is reading the mode at all — a config value that is read once at import
and not on `POST /admin/config` will silently leave the previous scenario's
setting in place, and you will conclude PKCE does not work. If
`replay-wrong-verifier` and `replay-no-verifier` return *different* statuses,
that is not necessarily a bug but it is an information leak worth noting in
the table. If `happy-path` itself fails, look at `redirect_uri`: it must be
byte-identical in the `/authorize` and `/token` requests, and a trailing
slash difference produces exactly the same rejection as an attack would. If
`code-reuse` reports the first token still valid, confirm you actually
re-sent the same code rather than a freshly issued one — the scenario is
worthless if the code differs.

## Answer before moving on

1. An attacker has a valid, unexpired authorization code and the full
   `/authorize` URL that produced it, including the `code_challenge`. Explain
   in one sentence, referring to a specific property of SHA-256, why they
   cannot complete the exchange.
2. `state` and `nonce` are both random values sent in the same request and
   both compared later. Give the concrete attack that each one stops, and
   say why checking only one leaves you exposed to the other.
3. PKCE was designed for mobile apps that cannot keep a client secret. OAuth
   2.1 requires it for confidential server-side clients too, which *do* have
   a secret. What threat does it address for those clients that the client
   secret does not?
4. Your service accepts an OIDC `id_token` as the bearer credential on its
   API. Name two distinct things that go wrong, one involving `aud` and one
   involving lifetime.

## Next up

[Topic 6 — SSRF: when your server makes requests an attacker chose](../06-ssrf/README.md).
Every topic so far has been about input reaching a parser. The next one is
about input reaching a **socket** — and about the network position your
server has that your attacker does not.
