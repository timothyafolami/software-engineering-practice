# Layer 7 · The shared lab

One `docker compose` stack for all eight topics. It **extends
`lab-harness/`** — the repo-wide stack (FastAPI + Postgres + Toxiproxy + k6
+ `grafana/otel-lgtm`) built in Block A — rather than starting a second one.
This page adds the four services security needs that no other layer wanted:
a **pooler in transaction mode**, a **Redis**, an **internal-only admin
service**, and a **fake cloud metadata endpoint**.

Topic READMEs reference the names on this page and do not restate them.
**Service names, environment variables, ports and script paths are a
contract the later code pass depends on. Change them here or not at all.**

```
docker compose \
  -f ../../lab-harness/compose.yml \
  -f compose.yml \
  up --build
```

Everything below that is not in `lab-harness/` lives in
`07-security/lab/`.

---

## Services

Inherited from `lab-harness/` and used as-is: `api` (FastAPI on uvicorn,
psycopg3, SQLAlchemy 2.x), `postgres`, `k6`, `toxiproxy`, `lgtm`.

| Service | Image / build | Why this layer needs it |
|---|---|---|
| `api` | inherited, rebuilt from `lab/api/` | The system under test. Every vulnerable/fixed handler variant is one build, selected at runtime by env — so a sweep never needs a rebuild |
| `postgres` | `postgres:18.6` | RLS policies, a non-owner role, real rows to leak |
| `pgbouncer` | `edoburu/pgbouncer` | **Transaction pooling mode.** Topic 1's `SET` vs `SET LOCAL` leak does not exist without connection reuse across requests |
| `redis` | `redis:7` | JWT denylist (Topic 4), authorization-code store (Topic 5), token-bucket rate limiter (Topic 8) |
| `internal-admin` | build `lab/internal-admin/` | A service on the compose network with **no published host port**. It is the thing SSRF reaches and you cannot. Serves `/secrets` |
| `metadata` | build `lab/metadata/` | Fake cloud metadata service. Serves `/latest/meta-data/iam/security-credentials/lab-role` and speaks IMDSv1 or IMDSv2 depending on `IMDS_VERSION` |
| `idp` | build `lab/idp/` | Minimal OAuth2/OIDC authorization server for Topic 5: `/authorize`, `/token`, `/introspect`, `/.well-known/openid-configuration`, JWKS |
| `rebind-dns` | build `lab/rebind-dns/` | Authoritative DNS for `*.rebind.lab.test`, alternating answers per lookup. Topic 6's DNS-rebinding payload |
| `collector` | build `lab/collector/` | Attacker-controlled endpoint. Counts bytes received. Topic 3's XSS exfiltration target |
| `k6` | `grafana/k6` v2.x | Load generator. **Open-model executors only** (`constant-arrival-rate`) — a closed-loop run cannot saturate the pool, and half this layer's numbers depend on the pool being saturated |

## Ports

Host ports are offset into a 07-only range so this stack can run beside
Layer 3's and Layer 5's without collision. **`internal-admin` and
`metadata` publish nothing** — that is the experiment, not an oversight.

| Service | In-container | Published on host |
|---|---|---|
| `api` | 8000 | 8007 |
| `idp` | 8000 | 8008 |
| `collector` | 8000 | 8009 |
| `postgres` | 5432 | 55437 |
| `pgbouncer` | 6432 | 56437 |
| `redis` | 6379 | 57379 |
| `internal-admin` | 8000 | — (network only) |
| `metadata` | 80 | — (network only) |
| `rebind-dns` | 53/udp | — (network only) |

## The network, and the one thing macOS changes

The stack declares a user-defined bridge network `secnet` with an explicit
subnet so the SSRF targets have stable addresses:

| Name | Address on `secnet` |
|---|---|
| `internal-admin` | `10.7.0.10` |
| `metadata` | `10.7.0.169` |
| `rebind-dns` | `10.7.0.53` |

On a real cloud instance the metadata service lives at the link-local
address **`169.254.169.254`**. This machine is **macOS 27 on arm64** and the
whole stack runs inside Docker Desktop's Linux VM, where binding a
link-local `169.254.0.0/16` subnet to a bridge network is not reliable. So
the lab uses `10.7.0.169` as the stand-in and the fixed validator in Topic 6
denies **both**: the real link-local range *and* the lab subnet. When you
read that deny set, read it as "loopback, RFC-1918, link-local, plus
whatever this environment's metadata address happens to be" — the
generalisation is the point, and hardcoding one IP is exactly the mistake
the topic is about.

Nothing in this layer reads `/proc` or `/sys/fs/cgroup`, so unlike Layer 1
there is no Linux-host-only step here. `curl` and `k6` run on the host
against published ports; anything aimed at an unpublished service runs
*inside* the network:

```
docker compose exec api curl -s http://internal-admin:8000/secrets   # works
curl -s http://localhost:.../secrets                                 # nothing to connect to
```

## Environment variables

All set on `api` unless noted. Every one is also writable at runtime through
`POST /admin/config` so a sweep is a loop, not a rebuild.

| Variable | Values (default first) | Topic |
|---|---|---|
| `LAB_DSN` | `postgresql://app_user:...@postgres:5432/sec_lab` | all |
| `LAB_POOLED_DSN` | `postgresql://app_user:...@pgbouncer:6432/sec_lab` | 1 |
| `LAB_DB_ROLE` | `app_user` \| `lab_owner` | 1 |
| `LAB_REDIS_URL` | `redis://redis:6379/0` | 4, 5, 8 |
| `HANDLER_MODE` | `vulnerable` \| `fixed_query` \| `fixed_rls` | 1 |
| `RLS_SET_MODE` | `local` \| `session` | 1 |
| `SQL_MODE` | `vulnerable` \| `parameterized` \| `parameterized_allowlist` | 2 |
| `TEMPLATE_ESCAPE` | `auto` \| `off` | 3 |
| `CSP_MODE` | `off` \| `allowlist` \| `nonce` | 3 |
| `TOKEN_STORE` | `local_storage` \| `http_only_cookie` | 3 |
| `JWT_STRATEGY` | `plain` \| `short_ttl_rotate` \| `denylist` \| `opaque_introspect` | 4 |
| `JWT_TTL_SECONDS` | `86400` | 4 |
| `JWT_ALG` / `JWT_ACCEPT_ALGS` | `RS256` / `RS256` | 4 |
| `PKCE_MODE` | `required` \| `optional` \| `off` | 5 (on `idp`) |
| `CODE_TTL_SECONDS` | `60` | 5 (on `idp`) |
| `CODE_SINGLE_USE` | `true` \| `false` | 5 (on `idp`) |
| `REDIRECT_URI_MATCH` | `exact` \| `prefix` | 5 (on `idp`) |
| `SSRF_MODE` | `vulnerable` \| `string_blocklist` \| `resolve_and_pin` | 6 |
| `IMDS_VERSION` | `v1` \| `v2` | 6 (on `metadata`) |
| `PASSWORD_HASH` | `argon2id` \| `bcrypt` \| `sha256` | 8 |
| `ARGON2_M_KIB` / `ARGON2_T` / `ARGON2_P` | `19456` / `2` / `1` | 8 |
| `COMPARE_MODE` | `constant_time` \| `naive_eq` | 8 |
| `RATELIMIT_MODE` | `off` \| `redis_token_bucket` \| `inproc` | 8 |
| `RATELIMIT_PER_MIN` | `10` | 8 |
| `WORKERS` | `1` | 8 (uvicorn workers; the in-process limiter multiplies by this) |

The argon2id defaults are OWASP's low-memory baseline
(m=19 MiB, t=2, p=1) from the
[Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html);
Topic 8 raises them and measures what it costs.

## Scripts

Two entry points, both in `lab/`. Topic READMEs invoke these and nothing
else.

**`seed.py`** — idempotent. Creates the schema, the roles, the RLS policies,
and the data:

```
python lab/seed.py                 # default scale
python lab/seed.py --scale full
python lab/seed.py --reset         # drop and recreate
```

| Object | Contents |
|---|---|
| roles | `lab_owner` (owns the tables, **bypasses RLS** — used only by the seeder) and `app_user` (`NOLOGIN`-less, non-owner, no `BYPASSRLS`; this is what `api` connects as) |
| `users` | 3 tenants: `alice`, `bob`, `carol`, with argon2id password verifiers |
| `invoices` | 1,500 rows, ids 1..1500, 500 owned by each tenant, **interleaved** so enumeration hits all three |
| `api_keys` | one 32-byte key per tenant, for Topic 8's timing comparison |
| RLS | `ENABLE ROW LEVEL SECURITY` on `invoices` with `USING (owner_id = current_setting('app.current_user')::int)` |

`app_user` being a non-owner is load-bearing: **a table owner bypasses RLS
silently by default**, so seeding and testing as the same role makes every
policy in this layer look like it works while enforcing nothing.

**`attack.sh`** — one dispatcher, `./attack.sh <topic> <scenario> [flags]`.
It sets the relevant env on `api` via `/admin/config`, runs the right
generator, and writes a CSV to `lab/out/<topic>-<scenario>.csv`.

| Invocation | What it runs |
|---|---|
| `./attack.sh idor vulnerable` | k6 `constant-arrival-rate`, 100 rps, 1,000 requests, enumerating `/invoices/{id}` as alice |
| `./attack.sh idor fixed_query` \| `fixed_rls` | same run, different `HANDLER_MODE` |
| `./attack.sh idor pooled-set` | two tenants concurrently through `pgbouncer` with `RLS_SET_MODE=session` |
| `./attack.sh sqli <payload>` | single-shot curl, prints status + row count |
| `./attack.sh xss <context>` | posts the payload, then reads the byte count from `collector` |
| `./attack.sh jwt <strategy>` | logout-then-poll revocation timer, plus a 200 rps p99 run on `/me` |
| `./attack.sh oauth <scenario>` | drives `/authorize` → callback → `/token`, then replays the code |
| `./attack.sh ssrf <payload>` | posts a URL to `/fetch`, records status and body bytes |
| `./attack.sh stuffing <mode>` | k6 credential-stuffing run against `/login` |

Every scenario prints the same trailer, and it is the row you paste into
`PREDICTIONS.md`:

```
scenario=<name>  requests=<n>  <metric>=<your number>  <unit>
```

## Version pins

Inherited from `lab-harness/`, restated because two of them matter here:
**Postgres 18.6** (RLS behaviour and `current_setting` semantics are what
Topic 1 tests), **Python 3.14**, **Node 24 LTS**, **Go 1.26.x**, **k6 v2.x**.
If your `k6` is on v1, the `constant-arrival-rate` options block differs and
the scripts will not parse.

## Teardown

```
docker compose -f ../../lab-harness/compose.yml -f compose.yml down -v
```

The `-v` matters: `postgres` keeps the seeded tenants in a named volume, and
a stale seed with every invoice owned by one tenant is the single most
common way Topic 1's experiment silently reports zero leaks.

## Next

[Topic 1 — Authentication vs. authorization, and IDOR](../01-authn-authz-and-idor/README.md),
or the layer [index](../README.md).
