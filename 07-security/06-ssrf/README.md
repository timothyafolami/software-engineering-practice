# Layer 7 · Topic 6 — SSRF: when your server makes requests an attacker chose

### The takeaway (read this first)

**The one idea:** the moment your service fetches a URL that came from user
input — a webhook, an image-from-URL, a link preview, a PDF renderer, an
RSS importer — the attacker can point it at things **your server can reach
and they cannot**: `localhost`, internal admin panels, other services on the
private network, and above all the **cloud metadata endpoint** that hands
out IAM credentials. That is SSRF, and in the OWASP Top 10:2025 it was
folded *into* A01 Broken Access Control — because it is, fundamentally, your
server's network position being borrowed by someone who does not have it.

**Why it matters in practice:** SSRF against a metadata service running the
legacy token-free protocol (IMDSv1) yields cloud credentials, and from there
the blast radius is the account, not the endpoint. This is the mechanism
behind the 2019 Capital One breach, and it remains one of the
highest-severity findings in any service that fetches URLs on request.

**You'll know it landed when:** any feature that takes a URL and fetches it
makes you immediately think "what internal thing can I make it hit," and you
can explain — without hand-waving — why
`if not url.startswith('http://internal')` is not a fix, and why even a
correct allowlist of hostnames is not one either.

## The concept

Your server sits inside a network boundary and can reach: loopback
(`127.0.0.0/8`, `::1`), RFC-1918 private ranges (`10/8`, `172.16/12`,
`192.168/16`), link-local (`169.254.0.0/16`, home of cloud metadata),
container and service-mesh addresses, and internal DNS names. An SSRF gives
the attacker a request *originating from inside that boundary*, with your
service's identity and often its credentials attached.

Defences fail in instructive ways, and the order of the failures is the
lesson:

- **Blocklists of strings** are trivially bypassed, and it is worth being
  able to list the bypasses from memory because the list explains *why* the
  approach is unfixable: `0.0.0.0`, bare `0`, `127.1`, decimal
  `2130706433`, octal `0177.0.0.1`, `[::1]`, `[::ffff:127.0.0.1]`, a
  hostname you control with an `A` record pointing at `127.0.0.1`, a
  userinfo trick like `http://expected.test@10.0.0.1/`, and a redirect from
  an allowed host. The set of *strings* that resolve to a forbidden
  *address* is not enumerable, because the mapping from one to the other is
  performed later, by someone else, using rules you do not control.
- **Allowlists of hostnames** are the right direction and still fall to
  two things. **Redirects:** an allowed URL returns `302` to
  `http://169.254.169.254/...` and your HTTP client follows it, because
  nearly every client follows redirects by default. **DNS rebinding:** you
  resolve the hostname, see an allowed address, approve it — and then the
  HTTP client resolves it *again* when it connects, and gets a different
  answer. Two lookups, state changed in between: a time-of-check to
  time-of-use race, the same class as any TOCTOU bug, with the attacker's
  DNS server as the thing that changes.
- **The layered fix that holds.** Resolve the hostname **yourself**, check
  **every** returned address against a deny set (loopback, private,
  link-local, the metadata address, your own subnets), then **connect to
  that exact validated IP** while passing the original `Host` header, so DNS
  cannot change under you. Re-run the whole validation **on every redirect
  hop**, or refuse redirects. Restrict the scheme to `http`/`https` (a
  surprising number of clients will happily do `file:`, `gopher:`, `dict:`).
  Then, underneath the code entirely: an **egress firewall** that blocks
  internal ranges so a code bug cannot reach them, and **IMDSv2**, where
  reading credentials requires a `PUT` to obtain a token first — which a
  GET-only SSRF cannot perform.

Note the shape of the final answer: no single layer is sufficient, and each
one has a known bypass. That is not a failure of the write-up; it is what
defence in depth actually means, and this is the clearest example of it in
the layer.

## How each language actually gets there

All six. **This is a runtime topic** — the subject is DNS resolution and
socket connect, and the six runtimes expose that seam at six genuinely
different depths, from "you cannot reach it" to "you are calling
`connect(2)` yourself."

**Python (`requests` / `httpx`).** Both follow redirects by default
(`allow_redirects=True`; `httpx` is the exception — it does *not* follow by
default, and knowing which of your two HTTP clients does is the point).
Neither exposes a resolver hook, so pinning means resolving with
`socket.getaddrinfo` yourself and then connecting to the literal IP with an
explicit `Host` header — or mounting a custom transport/adapter. The
common wrong pattern is a regex over the URL string, and it is wrong for the
reason above: the string is not the address.

**Node (`undici` / global `fetch`).** `undici`'s `Agent` accepts a custom
`connect` option, which is the correct place to pin: you get the hostname
and return a socket, so validation and connection happen in the same step
with no window between them. `fetch` follows redirects by default with
`redirect: 'follow'`; `'manual'` is one option away and is the right setting
for a fetcher.

**Go (`net/http`).** The most ergonomic correct answer in the lab.
`http.Client.CheckRedirect` is a function that receives every hop and can
re-run your validator or refuse, and `http.Transport.DialContext` receives
the resolved address at connect time — which means Go lets you validate the
*actual* address being connected to, closing the rebinding window
structurally rather than by pinning a string. Worth sitting with: Go is not
safer here by accident; someone designed those two hooks for exactly this.

**Rust (`reqwest` / `hyper`).** `reqwest::redirect::Policy` is an explicit
constructor argument — `Policy::none()` is a visible decision, not a flag
you might not know exists — and a custom `Resolve` implementation lets you
own DNS. The compile-time story is weaker here than in Topics 2 and 4:
nothing in the type system stops you fetching a bad URL, and it is worth
being precise that Rust's guarantees are about memory and data races, not
about network policy.

**C++ (libcurl).** The one talking to the network with nothing between:
`CURLOPT_FOLLOWLOCATION` is off by default (so C++ is accidentally *safer*
on the redirect axis than Python's `requests`), `CURLOPT_PROTOCOLS_STR`
restricts schemes explicitly, `CURLOPT_RESOLVE` pre-seeds the DNS cache with
an address you chose, and `CURLOPT_OPENSOCKETFUNCTION` hands you the socket
before `connect(2)` so you can inspect the sockaddr itself. Every abstraction
in the other five languages is a wrapper over these decisions, and reading
the curl option list is the fastest way to see the full set of knobs that
exist.

**Java (`java.net.http.HttpClient`).** `HttpClient.Redirect.NEVER` is the
default, which is the safe default and the opposite of Python's. The Java
hazard is historical and worth knowing because it still bites: the legacy
`java.net.URL` class performs **DNS resolution inside `equals()` and
`hashCode()`**, so putting URLs in a `HashSet` makes network calls and makes
two different hostnames "equal" if they resolve alike — a validation cache
keyed on `URL` can therefore be poisoned by DNS. Use `java.net.URI` for
anything that is not being fetched right now.

## The experiment

Uses the shared [`lab/`](../lab/README.md) stack, whose SSRF targets exist
only on the compose network: `internal-admin` at `10.7.0.10` serving
`/secrets`, `metadata` at `10.7.0.169` serving
`/latest/meta-data/iam/security-credentials/lab-role`, and `rebind-dns` at
`10.7.0.53` answering `*.rebind.lab.test` with a different address on every
lookup. Neither target publishes a host port — you can only reach them
through the bug.

**The macOS note that matters:** on a real cloud instance the metadata
address is `169.254.169.254`; Docker Desktop's Linux VM cannot reliably host
a link-local bridge subnet, so the lab uses `10.7.0.169` and the fixed
validator denies **both** the real link-local range and the lab subnet. If
you copy the deny set into production, keep both entries and add yours; a
validator that denies exactly one hardcoded metadata IP is the bug this
topic is about, in miniature.

`POST /fetch` takes a URL and returns the fetched body. `SSRF_MODE` selects
the defence: `vulnerable`, `string_blocklist` (denies the literal strings
`localhost`, `127.0.0.1`, `169.254.169.254`), `resolve_and_pin`.

Payload set, fired at all three modes:

| Payload | what it tests |
|---|---|
| `http://internal-admin:8000/secrets` | plain internal reach |
| `http://10.7.0.169/latest/meta-data/...` | credential theft |
| `http://0/secrets`, `http://2130706433/`, `http://[::1]:8000/` | blocklist bypass by encoding |
| `http://allowed.test/redirect-to-internal` | 302 into the private network |
| `http://a.rebind.lab.test/secrets` | DNS rebinding (TOCTOU) |
| `file:///etc/passwd`, `gopher://10.7.0.10:8000/_GET%20/` | scheme abuse |
| `http://ok.test@10.7.0.10/secrets` | userinfo confusion |

Measure per cell: **HTTP status**, **body bytes returned to the attacker**
(zero bytes is the only convincing "blocked" — a 200 with an empty body and
a 403 mean different things), and **time to response in ms**, because a
blind SSRF that returns nothing still leaks through timing.

Then the IMDS half: with `IMDS_VERSION=v1`, a GET reads credentials; with
`v2`, the same GET is refused without a token obtained by `PUT`. Record
bytes returned in both.

### How you'd know the fix is fake

**A validator that runs on the string and not on the connection.** It
resolves, checks, approves — and then hands the original *URL* to
`requests.get()`, which resolves again. Every payload in your test list is
blocked, because your test list contains no rebinding entry and your DNS
answers were stable. The tell is structural, not behavioural: if the code
that validates and the code that connects each perform their own name
resolution, there is a window between them regardless of what your tests
say. Look for the second resolution; do not look for a failing test.

The second fake: **an allowlist so broad it is a blocklist**. If
`resolve_and_pin` blocks every external URL including legitimate ones, you
have denied all addresses rather than the private ones, and the experiment
will report a perfect score for a feature that no longer works.

## How to run

Each program runs the full payload set through both defences and prints, per
payload, the `string_blocklist` verdict, the `resolve_and_pin` verdict, and the
resolved address — so the bypass is visible as a column, not a claim. All six
are stdlib-only and run offline (no Docker, no network): the DNS resolver is
modelled, because the point is that a name maps to an ADDRESS, which is the
thing that must be checked. Every language reports `string_blocklist: 7/7`
reached, `resolve_and_pin: 0/7`.

```
python3 python/ssrf.py
node   nodejs/ssrf.js
cd golang && go run ssrf.go && cd ..
cd java   && javac Ssrf.java -d /tmp/t6java && java -cp /tmp/t6java Ssrf && cd ..
g++ -O2 -std=c++17 -o /tmp/cpp_ssrf cpp/ssrf.cpp && /tmp/cpp_ssrf
cd rust && cargo run && cd ..
```

The live half — actually connecting to the unpublished `internal-admin` /
`metadata` targets, the 302-into-the-private-network redirect, the real
alternating `rebind-dns`, and IMDS v1 vs v2 byte counts — needs the compose
stack, where the targets exist only on `secnet`. That is where you confirm the
premise (`docker compose exec api curl -s http://internal-admin:8000/secrets`
works while the same URL is unreachable from the host). The programs above
settle the validator's logic; the rebinding TOCTOU is why that validator must
pin the resolved address rather than re-resolve.

## Predict, then record

1. Which bypass do you expect to defeat `string_blocklist` first — `http://0/`,
   the decimal IP, or the redirect? Rank them, then say why the redirect is
   categorically harder to stop than the other two.
2. Against `resolve_and_pin`, what happens on the rebinding domain, and how
   many requests does it take before a leak appears (if it appears)? Name the
   mechanism that decides.
3. With `IMDS_VERSION=v2`, how many bytes of credential does the GET-only
   SSRF return, and what exactly is the token requirement stopping — the
   read, or the request?

| Payload | `vulnerable`: status / body bytes | `string_blocklist`: status / body bytes | `resolve_and_pin`: status / body bytes |
|---|---|---|---|
| internal admin host |  |  |  |
| metadata address |  |  |  |
| `http://0/` |  |  |  |
| decimal `2130706433` |  |  |  |
| `[::1]` |  |  |  |
| redirect from allowed host |  |  |  |
| DNS rebinding |  |  |  |
| `file://` scheme |  |  |  |
| userinfo `@` |  |  |  |

| IMDS measurement | `v1` | `v2` |
|---|---|---|
| credential bytes returned to attacker |  |  |
| requests needed |  |  |
| response time, ms |  |  |

| Rebinding detail | value |
|---|---|
| requests before the first leak |  |
| DNS TTL served by `rebind-dns`, seconds |  |
| leak rate per 100 requests at that TTL |  |

**What would mean the experiment is broken, not the prediction:** if the
vulnerable endpoint cannot reach `internal-admin` at all, your compose
network segmentation is wrong — run the `docker compose exec` check above
before concluding anything. If the redirect payload does not work against
`vulnerable`, confirm your HTTP client follows redirects (`httpx` and Java's
`HttpClient` do not by default; `requests` does). If the rebinding payload
never leaks, your resolver is caching the first answer — check the TTL
`rebind-dns` is serving and whether the runtime keeps its own DNS cache
(the JVM's `networkaddress.cache.ttl` caches successful lookups by default,
which will *hide* this bug in Java specifically). If `resolve_and_pin`
blocks legitimate external URLs, your deny set is too broad.

## Answer before moving on

1. Explain in one sentence why a URL-string blocklist can never be complete,
   using the phrase "the set of strings that resolve to a forbidden
   address."
2. IP-pinning defeats DNS rebinding. What general class of bug is rebinding
   an instance of, and where else in this repo have you seen that class?
   (Hint: two lookups with state changing in between — Layer 3 and Layer 4
   both have one.)
3. Your validator is perfect and redirects are off. Name one thing an
   attacker can still do with a URL-fetching feature that no per-request URL
   validation addresses. (Hint: it is not about *where* the request goes.)
4. IMDSv2 requires a `PUT` to get a token. Explain why that specific choice
   — a different HTTP method, rather than a header or a password — stops the
   overwhelming majority of SSRF, and name the kind of SSRF it does not stop.

## Next up

[Topic 7 — Secrets and supply chain](../07-secrets-and-supply-chain/README.md).
So far the attacker has been sending you requests. Next: the attacker whose
code you installed on purpose, and the credential you pushed to a public
repository last year.
