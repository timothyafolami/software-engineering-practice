# Layer 2 · Topic 4 — Keep-alive across a load balancer

### The takeaway (read this first)

**The one idea:** HTTP keep-alive is a *mutual* agreement about an idle
connection that neither side is obliged to honour, and when the two ends
disagree about who closes first you get a request written into a socket the
peer has already closed — an intermittent 502 that no single component's logs
can explain, because no single component did anything wrong.

**Why it matters in practice:** uvicorn's `--timeout-keep-alive` default is
5 seconds. An AWS Application Load Balancer's default idle timeout is 60
seconds. Deploy FastAPI behind an ALB, change nothing, and you have this bug
right now, at a low rate, and it looks like random flakiness. Both numbers are
their vendors' documented defaults — check `uvicorn --help` and the ELB
documentation rather than this page, because defaults move.

**You'll know it landed when:** you can state the timeout ordering rule for
any proxy/backend pair from memory, and narrate the exact packet sequence that
produces the 502.

## The concept

The rule is one line: **the backend's idle timeout must be strictly longer
than the proxy or load balancer's.**

In packets, with the default ordering (backend 5 s, LB 60 s):

```
t=0     LB finishes a request, keeps the connection in its pool, idle
t=5s    backend's keep-alive timer fires  →  backend sends FIN
t=5.001 a new request arrives at the LB
        LB picks that connection from its pool — it has not processed the
        FIN, or it raced it — and writes the request
t=5.002 backend is half-closed, cannot accept a new request  →  RST
        LB has no response, and no safe way to know whether the request
        was processed  →  502 to the client
```

Nothing logged an error on the backend. The backend correctly closed an idle
connection, which is what you asked it to do. The LB correctly reported that
it could not complete the request. The bug lives in the gap between them, and
that is why the ordering rule exists: whoever closes first must be the side
that is *not* holding a pool of connections it is about to reuse.

The client-side mirror image is the same event and you have already seen it:
`requests` pulls a connection from urllib3's pool that the server — or an idle
NAT gateway, or a stateful firewall with its own idle timer — already dropped,
and you get `RemoteDisconnected: Remote end closed connection without response`
or a bare `ConnectionResetError`. urllib3 retries this for idempotent methods;
for POST it will not, and should not, because it cannot know whether the
request was processed.

Two more knobs bite here.

**`keepalive_requests`.** nginx and most proxies close a connection after N
requests on purpose — nginx's default is 1000 in current releases; confirm with
your build's documentation. This is a feature, not a limit: a pool of immortal
connections never discovers a new backend, so DNS changes, scale-ups and
rolling deploys never take effect for that client. Topic 5 is the whole story;
here you just need to know the knob exists and why bounding it costs
handshakes on purpose.

**TCP keepalive is not HTTP keep-alive.** Different layer, different purpose,
confusingly similar name. `SO_KEEPALIVE` with `TCP_KEEPIDLE` set well under
your NAT or LB idle drop is how you *detect* a connection that has silently
died, rather than discovering it the next time you try to use it. Most Python
clients do not enable it; urllib3 2.x accepts `socket_options` if you want to.

## How each language actually gets there

The mechanism here mostly lives outside the language — it is a property of two
independent idle timers on either end of a TCP connection. What differs per
runtime is the **server-side default**, and that is what decides whether you
ship the bug. Four runtimes cover the space; **Rust and C++ are omitted
because hyper and a hand-rolled server would only restate the same single
knob, with no new mechanism to show.**

**Python (uvicorn).** `--timeout-keep-alive` defaults to 5 seconds, which is
shorter than every load balancer default you will meet. This is the origin of
the bug in the FastAPI stack: the safest small change you can make to a
FastAPI service behind an ALB is to raise this above the LB's idle timeout,
and it is one flag.

**Node.js.** Node is the instructive case because the ordering constraint is
*internal* as well as external: `server.keepAliveTimeout` must be shorter than
`server.headersTimeout`, or you get the identical race entirely inside one
process — the socket's keep-alive timer fires while the header-read timer
still believes a request may arrive. Node has shipped defaults where these two
values relate correctly, but any deployment that sets one and not the other
recreates the race. Print both before you tune either:
`node -e "const s=require('http').createServer(); console.log(s.keepAliveTimeout, s.headersTimeout, s.requestTimeout)"`.

**Go.** `http.Server.IdleTimeout`, when zero, falls back to `ReadTimeout`; if
that is zero too, there is **no idle timeout at all**, which is documented
behaviour in `net/http`. So a default Go server never closes idle connections
and the load balancer is always the side that closes first — the correct
ordering, arrived at by accident rather than by design. Worth sitting with:
Go's default is safe *for this bug* and unsafe for connection leaks, which is
the same trade Topic 2 found at the database pool. "No limit" keeps choosing
which component fails.

**Java.** Servlet containers expose the same knob under different names —
Tomcat's `keepAliveTimeout` (which falls back to `connectionTimeout` when
unset) and its `maxKeepAliveRequests`, which is the direct analogue of nginx's
`keepalive_requests`. The JVM lesson is that there are usually *two* layers
holding an idle timer — the container and any embedded reverse proxy or
service mesh sidecar — so "the backend's timeout" is not a single number until
you have gone and read both.

## The experiment

`lab/topic4/` — nginx (`lb`) in front of uvicorn (`api`), with k6 driving a
low, **bursty** rate. The bug needs *idle* time, so high sustained load hides
it completely, and that alone is one of the lessons of the topic: this is a
defect that gets more likely as your traffic gets quieter.

Three configurations, selected with `KEEPALIVE_PROFILE`:

| `KEEPALIVE_PROFILE` | Backend idle | LB idle | Expectation |
|---|---|---|---|
| `mismatched` | 5 s | 60 s | the default deployment — count the 502s |
| `ordered` | 75 s | 60 s | correct ordering |
| `ordered_bounded` | 75 s | 60 s, `keepalive_requests` bounded | correct ordering, and connections still rotate |

Run `tcpdump` in the `sniff` sidecar throughout and **find one instance**: a
FIN from the backend, then a request from nginx on that same four-tuple, then
an RST. Seeing that sequence once, with your own timestamps on it, is the
entire point of this topic. Everything else is bookkeeping.

For the third configuration, confirm rotation rather than assuming it: count
distinct four-tuples in the capture over a fixed window and check that
connections are being retired at roughly the rate `keepalive_requests`
predicts.

## How to run

```
cd 02-network/lab
KEEPALIVE_PROFILE=mismatched docker compose up -d lb api
docker compose exec sniff tcpdump -i any -n 'tcp port 8000 and (tcp[tcpflags] & (tcp-fin|tcp-rst) != 0)' -w /caps/topic4.pcap &
docker compose run --rm load run /scripts/topic4.js
docker compose exec lb sh -c "grep ' 502 ' /var/log/nginx/access.log | wc -l"
```

`topic4.js` targets `http://lb:8080`, not `http://api:8000`, and prints the
target it resolved in its `setup()` line — read that line before the run. It
is the only script in the layer that must not go straight to `api`: without
nginx in the path there is no second pool and no second idle timer, so there
is nothing for the backend's `FIN` to race, and the run comes back clean.

Then repeat with `KEEPALIVE_PROFILE=ordered` and
`KEEPALIVE_PROFILE=ordered_bounded`. The capture lands in `lab/caps/` on the
host; open it in Wireshark **[host]** and filter to the four-tuple you found.

The per-runtime version, without the load balancer. Each of the four programs
builds the same thing out of raw sockets — a backend with an idle timer and a
pool that reuses a connection after an idle gap — reproduces the failure, then
fixes it by changing one number, and prints the defaults it can actually read
off the runtime you are running:

```
python3 python/idle_timeout_defaults.py
node nodejs/keepalive_vs_headers_timeout.js
cd golang && go run no_idle_timeout_by_default.go
cd java && javac IdleTimersOnBothSides.java -d /tmp/javabuild && \
  java -cp /tmp/javabuild IdleTimersOnBothSides
```

All four are standard library only; the Python one additionally reports your
installed uvicorn's `timeout_keep_alive` if uvicorn is importable. None of them
quote a load balancer's number, because that half is not readable from your
machine and must come from your own console or nginx config — both numbers or
neither.

Two findings from those programs are worth knowing before you run the compose
stack, because each one can make a run look clean when it is actually broken.
Node's server sends its FIN roughly a second *later* than the configured
`keepAliveTimeout` on the build measured here, so an idle gap chosen from the
documented number can miss the race entirely. And a client written in Node or
Go will quietly notice the peer's FIN and dial again — which is why all four
programs hold a raw socket instead: to see what a proxy's pool sees, you have
to hold the socket the way a proxy's pool holds it.

## Predict, then record

- 502 rate with mismatched timeouts, at roughly 10 rps bursty over 10 min: ______
- Does the 502 rate go **up** or **down** as load increases? ______
  (Think carefully before you answer — this is the tell.)
- Under `ordered_bounded`, how many distinct connections per minute? ______

| Config | requests | 502s | 502 rate | FIN→request→RST found in pcap? |
|---|---|---|---|---|
| mismatched (backend 5 s / LB 60 s) | | | | |
| ordered (backend 75 s / LB 60 s) | | | | |
| ordered_bounded | | | | |

| Runtime | server idle-timeout default | source you checked |
|---|---|---|
| uvicorn | | |
| Node http.Server | | |
| Go net/http.Server | | |
| Tomcat | | |

**What would mean the experiment is broken, not the prediction wrong:**

- Zero 502s under `mismatched` → your load has no idle gaps. The connection
  must sit idle *past* the backend timeout and *before* the LB's. Add sleeps
  between iterations; a continuous 500 rps will never reproduce this.
  **But check the pcap before you conclude the run was bad.** With `nginx` as
  the LB, idle gaps are necessary and not sufficient: nginx keeps a read event
  armed on every connection sitting in its upstream keepalive pool, so when the
  backend's `FIN` arrives it discards that connection instead of writing the
  next request onto it. The race window is the microseconds between *choosing*
  a cached connection and *writing* to it, and nginx usually wins. What the
  capture will show under `mismatched` is the thing the 502 is only a symptom
  of — **who closes first**: every `F` on port 8000 goes `Out` from `api`,
  because the backend's 5 s timer beats the proxy's 60 s one. Under `ordered`
  there are no `F`s at all in a busy run, and under `ordered_bounded` every
  `F` comes `In` from nginx, which is `keepalive_requests` rotating the pool
  deliberately. That direction flip is the finding; the 502 is what happens
  when the proxy in front of you does *not* watch its idle sockets.
- 502s at the same rate in *all three* configs → something else is generating
  them: a backend crash, an OOM kill, a refused upstream. Read the backend
  logs before blaming keep-alive.
- An empty pcap → the sniffer is not in `api`'s network namespace. Verify
  `network_mode: "service:api"` in the compose file and capture on `-i any`.
- You see RSTs but never a preceding FIN on the same four-tuple → you are
  looking at a different failure (connection refused, or a reset from the LB
  side). Match the four-tuple before concluding anything.
- 502 count from the nginx log disagrees with k6's error count by a large
  factor → nginx is retrying to another upstream. Check `proxy_next_upstream`;
  that retry is the LB quietly hiding the bug from your client and from you.

## Answer before moving on

1. Why does the 502 rate *fall* as traffic rises, and what does that imply
   about ever reproducing this bug in a staging load test?
2. The LB cannot know whether the backend processed the request it wrote into
   a half-closed socket. When is it safe for the LB to retry anyway, and what
   must your API provide for that to be true?
3. Bounding `keepalive_requests` costs handshakes on purpose. Argue for a
   specific number for a service behind a Kubernetes Service whose pods scale
   every few minutes, and say what you would measure to know you chose badly.
4. Go's server has no idle timeout by default, which happens to give the
   correct ordering here. Describe the incident that same default causes, and
   say what you would set it to given that both failures exist.

## Next up

[Topic 5 — DNS, TTLs, and the pod that kept talking to a dead IP](../05-dns-ttls-and-the-dead-ip/README.md):
the connection you just decided to keep alive forever is pinned to an IP
address, and that address is about to stop being correct.
