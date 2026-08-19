# Layer 7 · Security, at mechanism level

You already care about this, which puts you ahead. The upgrade this layer
buys you is the move from *following practices* to *understanding
mechanisms* — because a rule you cannot derive stops protecting you the
moment the situation is unusual, and the interesting ones always are.

Eight topics. All the teaching content lives in the topic READMEs below;
this page is the index.

| # | Topic | Folder | Languages |
|---|---|---|---|
| 1 | Authentication vs. authorization, and IDOR | [`01-authn-authz-and-idor/`](01-authn-authz-and-idor/README.md) | Python, Node, Go, Java |
| 2 | SQL injection as a string-building failure | [`02-sql-injection/`](02-sql-injection/README.md) | all six |
| 3 | XSS by output context, and where you must never put a token | [`03-xss-and-output-context/`](03-xss-and-output-context/README.md) | Python, Node, Go |
| 4 | What a JWT is, and the revocation problem you cannot wish away | [`04-jwt-and-revocation/`](04-jwt-and-revocation/README.md) | Python, Node, Go, Java, Rust |
| 5 | OAuth2, OIDC, and PKCE end to end | [`05-oauth2-oidc-and-pkce/`](05-oauth2-oidc-and-pkce/README.md) | Python, Node, Go |
| 6 | SSRF: when your server makes requests an attacker chose | [`06-ssrf/`](06-ssrf/README.md) | all six |
| 7 | Secrets and supply chain | [`07-secrets-and-supply-chain/`](07-secrets-and-supply-chain/README.md) | all six |
| 8 | Crypto hygiene and rate limiting | [`08-crypto-and-rate-limiting/`](08-crypto-and-rate-limiting/README.md) | all six |

Run Topics 1 and 2 first if you are shipping to production now: they are the
two live bugs most likely to already be in your service, and each is one
evening.

## The shared lab

One `docker compose` stack for all eight topics, **extending the repo-wide
`lab-harness/`**: FastAPI + Postgres + PgBouncer in transaction mode + Redis,
plus an internal-only admin service and a fake metadata endpoint for the SSRF
work. It also defines `seed.py` and `attack.sh`, which every topic's run block
invokes. Build it once: **[`lab/README.md`](lab/README.md)**.

## Why the language set varies here

The repo's rule is *pick the languages that make the mechanism visible, and
say why whenever you use fewer than six.* This layer splits down the middle.

Where the mechanism is a **missing check** — IDOR, an unverified `state` —
the language is nearly irrelevant, so those topics keep only the runtimes
whose *frameworks* differ in what they make feel already-handled. Where the
mechanism lives in the **runtime** — how a driver binds a parameter on the
wire, whether an HTTP client re-resolves DNS before connecting, whether the
JIT undoes your constant-time comparison, what a package manager runs at
install time — all six earn their place, and the contrast is the finding.
Each topic states its own reason in its own first language paragraph.

## How this layer is graded, which is different

In Layers 1–6 a wrong prediction was cheap. Here, a wrong prediction *that
you deploy* is an incident with your name on the postmortem, so "I ran it
and it seemed fine" is not evidence of safety — the attacker's input is not
in your test suite. Every topic carries a **"how you'd know the fix is
fake"** section, and every record table asks for a measured quantity (rows
leaked per 1,000 requests, revocation latency in ms, attempts to first
success) precisely so that "seemed fine" cannot be an answer.

## The roadmap's ownership test

> You can look at a new feature and articulate the attacker's best move
> against it, unprompted, before it ships.

Not "you ran a scanner." Not "you added `helmet`." You, in the design
review, saying *here is the request an attacker sends, here is what they
get, here is the line that lets them.* Every experiment in the eight topics
is a rep at that, and the layer is not finished until you run it on your own
service: draw the trust boundaries, walk **STRIDE** at each one (spoofing,
tampering, repudiation, information disclosure, denial of service, elevation
of privilege), and for every "yes, an attacker could," write the
two-sentence explain-back — *why this is exploitable, and what input proves
it.* If the second sentence will not come, you do not yet understand the
threat, which is the signal to go build the small reproduction. Prefer the
control that lives at the data or query layer, for the reason Topic 1
measures.

## Resources

- [OWASP Top 10:2025](https://owasp.org/Top10/) — read A01, A03 and A05 as
  mechanism writeups. The category numbering used across this layer comes
  from here and is the thing most likely to go stale; check it.
- [OWASP Cheat Sheet Series](https://cheatsheetseries.owasp.org/) — Password
  Storage (source of Topic 8's argon2id parameters), CSP, SQLi, OAuth2.
- **RFC 9700 / BCP 240** and the OAuth 2.1 draft (Topics 4-5); **RFC 7636**
  (PKCE), **RFC 9449** (DPoP), **RFC 7662** (introspection).
- **PEP 751** (`pylock.toml`) and the `uv` docs — Topic 7's current state.

## Next up

Build the [lab](lab/README.md), then Topic 1 end to end. After this layer the
roadmap turns to craft — Layer 8, complexity as the only enemy.
