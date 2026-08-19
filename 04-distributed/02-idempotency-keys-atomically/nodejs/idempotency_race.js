// Layer 4 Topic 2 -- the same three handlers as python/idempotency_race.py, in Node.
//
// WHAT THIS DEMONSTRATES: node-postgres surfaces the unique violation as
// `err.code === '23505'`, character for character the same string Go's pgx and
// Python's psycopg give you. The mechanism is in Postgres, not in the language.
//
// Node hosts implementation A for a reason. "I will just check first" reads most
// natural in JavaScript, sits in the most codebases, and is the most wrong -- and
// the shape below (await the SELECT, then await the effect) is exactly how it
// gets written, because `await` looks like it serialises things and does not.
//
// WHAT TO LOOK FOR IN THE OUTPUT: the DUPLICATE CHARGES line, and the fact that
// nothing in the JavaScript is at fault. Every `await` is correct, every error is
// handled, and A still charges the card five times.
//
//   npm install                       # once, in this directory
//   node idempotency_race.js --impl A --keys 200 --concurrency 5

'use strict';

const crypto = require('node:crypto');
const { Client } = require('pg');

const TENANT = 'acme';
const DSN = process.env.LAB_DSN || 'postgresql:///sep_lab_04_dist';

// gen_random_uuid(), not uuidv7(): uuidv7() is a Postgres 18 function and the
// local fallback server is 17.5 (see lab/README.md). That costs B-tree insert
// locality, so no insert-throughput number from a fallback run is comparable
// with a container run.
const DDL = `
CREATE TABLE IF NOT EXISTS idempotency_keys (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     text        NOT NULL,
    key           text        NOT NULL,
    fingerprint   text        NOT NULL,
    state         text        NOT NULL
                  CHECK (state IN ('in_flight', 'succeeded', 'failed_permanently')),
    response      jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL DEFAULT now() + interval '24 hours',
    UNIQUE (tenant_id, key)
);
CREATE TABLE IF NOT EXISTS charges (
    id              bigserial PRIMARY KEY,
    run_id          text        NOT NULL,
    impl            text        NOT NULL,
    tenant_id       text        NOT NULL,
    idempotency_key text        NOT NULL,
    amount_cents    integer     NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
    -- NO UNIQUE (tenant_id, idempotency_key). On purpose; see the Python header.
);
CREATE INDEX IF NOT EXISTS charges_run_idx ON charges (run_id);`;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function fingerprint(body) {
  const canonical = JSON.stringify(body, Object.keys(body).sort());
  return crypto.createHash('sha256').update(`POST /payments ${canonical}`).digest('hex');
}

// --------------------------------------------------------------- the handlers
// Each returns [status, note]. `holdMs` is a deliberate delay inside the
// winner's transaction, applied identically in all three. It changes none of
// them; it widens a window that is otherwise microseconds wide.

// A -- check-then-insert. Three separate transactions, and the effect happens
// BEFORE the key row is recorded, because charge_the_card() is an HTTP call to a
// processor with no transaction to join and no way to roll back. Putting the two
// in one transaction is implementation B's structural rule.
async function handleA(c, runId, key, body, holdMs) {
  const seen = await c.query(
    'SELECT state FROM idempotency_keys WHERE tenant_id = $1 AND key = $2',
    [TENANT, key]
  );
  // Both concurrent requests reach here. Under READ COMMITTED neither sees the
  // other's uncommitted insert, and `await` did not serialise anything -- it
  // yielded this one connection's turn, not everybody else's.
  if (seen.rowCount > 0) return [200, 'replay'];

  if (holdMs) await sleep(holdMs);
  await c.query(
    `INSERT INTO charges (run_id, impl, tenant_id, idempotency_key, amount_cents)
     VALUES ($1, 'A', $2, $3, $4)`,
    [runId, TENANT, key, body.amount_cents]
  );
  // The money has moved. Only now does the unique index object.
  try {
    await c.query(
      `INSERT INTO idempotency_keys (tenant_id, key, fingerprint, state)
       VALUES ($1, $2, $3, 'succeeded')`,
      [TENANT, key, fingerprint(body)]
    );
  } catch (err) {
    if (err.code === '23505') {
      return [500, '23505 AFTER charging -- the card was already charged'];
    }
    throw err;
  }
  return [201, 'charged'];
}

// B -- atomic insert. Key row and effect commit in the SAME transaction.
async function handleB(c, runId, key, body, holdMs) {
  const fp = fingerprint(body);
  await c.query('BEGIN');
  try {
    const claim = await c.query(
      `INSERT INTO idempotency_keys (tenant_id, key, fingerprint, state)
       VALUES ($1, $2, $3, 'in_flight')
       ON CONFLICT (tenant_id, key) DO NOTHING`,
      [TENANT, key, fp]
    );
    // ON CONFLICT DO NOTHING RETURNING id gives back ZERO rows on conflict, so
    // RETURNING never hands you the row that already exists. "Did I win?" is
    // rowCount === 1, and the loser must SELECT separately.
    //
    // And if we lost, that INSERT already BLOCKED on the unique index until the
    // winner committed or rolled back. It did not fail fast. A duplicate's
    // latency is bounded below by the winner's entire transaction.
    if (claim.rowCount === 1) {
      if (holdMs) await sleep(holdMs);
      const charge = await c.query(
        `INSERT INTO charges (run_id, impl, tenant_id, idempotency_key, amount_cents)
         VALUES ($1, 'B', $2, $3, $4) RETURNING id`,
        [runId, TENANT, key, body.amount_cents]
      );
      await c.query(
        `UPDATE idempotency_keys SET state = 'succeeded', response = $1
         WHERE tenant_id = $2 AND key = $3`,
        [JSON.stringify({ charge_id: charge.rows[0].id, status: 'succeeded' }), TENANT, key]
      );
      await c.query('COMMIT');
      return [201, 'charged'];
    }

    const existing = await c.query(
      `SELECT state, fingerprint FROM idempotency_keys
       WHERE tenant_id = $1 AND key = $2`,
      [TENANT, key]
    );
    await c.query('COMMIT');
    const row = existing.rows[0];
    // Same key, different body. Replaying the stored response here would tell
    // the caller that a request which never existed had succeeded.
    if (row.fingerprint !== fp) return [422, 'fingerprint mismatch'];
    if (row.state === 'succeeded') return [200, 'replay'];
    if (row.state === 'failed_permanently') return [409, 'previous attempt failed permanently'];
    // Still in_flight and we are here: the winner rolled back, so nobody owns
    // this key. Retryable, and it has to say so.
    return [409, 'in flight, retry'];
  } catch (err) {
    await c.query('ROLLBACK');
    throw err;
  }
}

// C -- advisory lock. Correct too, different cost profile.
async function handleC(c, runId, key, body, holdMs) {
  await c.query('BEGIN');
  try {
    // xact, not session: a session-level advisory lock behind pgbouncer in
    // transaction pooling mode outlives your ownership of the server connection.
    await c.query('SELECT pg_advisory_xact_lock(hashtext($1))', [`${TENANT}:${key}`]);
    const seen = await c.query(
      'SELECT state FROM idempotency_keys WHERE tenant_id = $1 AND key = $2',
      [TENANT, key]
    );
    if (seen.rowCount > 0) {
      await c.query('COMMIT');
      return [200, 'replay'];
    }
    await c.query(
      `INSERT INTO idempotency_keys (tenant_id, key, fingerprint, state)
       VALUES ($1, $2, $3, 'succeeded')`,
      [TENANT, key, fingerprint(body)]
    );
    if (holdMs) await sleep(holdMs);
    await c.query(
      `INSERT INTO charges (run_id, impl, tenant_id, idempotency_key, amount_cents)
       VALUES ($1, 'C', $2, $3, $4)`,
      [runId, TENANT, key, body.amount_cents]
    );
    await c.query('COMMIT');
    return [201, 'charged'];
  } catch (err) {
    await c.query('ROLLBACK');
    throw err;
  }
}

const HANDLERS = { A: handleA, B: handleB, C: handleC };

// ------------------------------------------------------------------ harness

function parseArgs(argv) {
  const args = { impl: null, keys: 200, concurrency: 5, holdMs: 10, varySlot: -1, reset: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--impl') args.impl = argv[++i];
    else if (a === '--keys') args.keys = Number(argv[++i]);
    else if (a === '--concurrency') args.concurrency = Number(argv[++i]);
    else if (a === '--hold-ms') args.holdMs = Number(argv[++i]);
    else if (a === '--vary-slot') args.varySlot = Number(argv[++i]);
    else if (a === '--reset') args.reset = true;
    else {
      console.error(`unknown argument: ${a}`);
      process.exit(2);
    }
  }
  if (!HANDLERS[args.impl]) {
    console.error('usage: node idempotency_race.js --impl A|B|C ' +
      '[--keys N] [--concurrency N] [--hold-ms N] [--vary-slot N] [--reset]');
    process.exit(2);
  }
  return args;
}

/** A reusable N-party barrier. Every slot leaves at the same instant, which is
 *  the entire experiment -- firing duplicates sequentially tests nothing. */
function makeBarrier(parties) {
  let waiting = 0;
  let release;
  let gate = new Promise((r) => { release = r; });
  return function arrive() {
    const mine = gate;
    if (++waiting === parties) {
      waiting = 0;
      const open = release;
      gate = new Promise((r) => { release = r; });
      open();
    }
    return mine;
  };
}

function percentile(values, q) {
  if (values.length === 0) return NaN;
  const s = [...values].sort((a, b) => a - b);
  const i = Math.min(s.length - 1, Math.max(0, Math.round(q * s.length + 0.5) - 1));
  return s[i];
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const handler = HANDLERS[args.impl];

  const admin = new Client({ connectionString: DSN, application_name: 'sep-layer4-lab' });
  try {
    await admin.connect();
  } catch (err) {
    console.error(`cannot reach ${DSN}: ${err.message}\n\n` +
      'The local fallback needs a Postgres that is already listening.\n' +
      'Check with: python3 ../../lab/local/check_env.py');
    process.exit(1);
  }
  await admin.query(DDL);
  if (args.reset) {
    await admin.query('TRUNCATE charges, idempotency_keys');
    console.log('[setup] truncated charges and idempotency_keys');
  }
  const version = (await admin.query("SELECT split_part(version(), ' on ', 1) AS v")).rows[0].v;

  const runId = `${args.impl}-node-${crypto.randomBytes(4).toString('hex')}`;
  const keys = Array.from({ length: args.keys }, (_, i) => `${runId}-key-${String(i).padStart(5, '0')}`);

  console.log('\n==============================================================================');
  console.log(`Topic 2 (Node) -- IMPL ${args.impl}   ${args.keys} keys x ${args.concurrency} simultaneous requests`);
  console.log('==============================================================================');
  console.log(`  server        : ${version}`);
  console.log(`  run id        : ${runId}`);
  console.log(`  hold in txn   : ${args.holdMs} ms (identical across A, B and C)`);
  if (args.varySlot >= 0) {
    console.log(`  vary slot     : ${args.varySlot} sends a different body under the same key`);
  }
  console.log('  charges index : NO unique constraint on idempotency_key -- deliberate');

  const arrive = makeBarrier(args.concurrency);
  const results = [];
  const errors = [];

  const clients = await Promise.all(
    Array.from({ length: args.concurrency }, async () => {
      const c = new Client({ connectionString: DSN, application_name: 'sep-layer4-lab' });
      await c.connect();
      return c;
    })
  );

  const t0 = process.hrtime.bigint();
  await Promise.all(clients.map(async (c, slot) => {
    for (let i = 0; i < keys.length; i++) {
      await arrive();
      // --vary-slot: same key, DIFFERENT body -- a client that reused an
      // idempotency key for a new request. Only B stores a fingerprint and can
      // tell; A and C replay the wrong thing at 200.
      const body = slot === args.varySlot
        ? { amount_cents: 999999, currency: 'GBP' }
        : { amount_cents: 4200 + i, currency: 'GBP' };
      const started = process.hrtime.bigint();
      let status, note;
      try {
        [status, note] = await handler(c, runId, keys[i], body, args.holdMs);
      } catch (err) {
        status = 500;
        note = err.code ? `${err.code} ${err.message}` : err.message;
        errors.push(note);
      }
      const ms = Number(process.hrtime.bigint() - started) / 1e6;
      results.push({ status, note, ms });
    }
  }));
  const wall = Number(process.hrtime.bigint() - t0) / 1e9;
  await Promise.all(clients.map((c) => c.end()));

  const q = async (sql) => (await admin.query(sql, [runId])).rows[0];
  const dupKeys = (await q(`SELECT count(*)::int AS n FROM (SELECT 1 FROM charges
      WHERE run_id = $1 GROUP BY idempotency_key HAVING count(*) > 1) d`)).n;
  const extra = (await q(`SELECT coalesce(sum(c - 1), 0)::int AS n FROM (SELECT count(*) c
      FROM charges WHERE run_id = $1 GROUP BY idempotency_key) d`)).n;
  const total = (await q('SELECT count(*)::int AS n FROM charges WHERE run_id = $1')).n;

  console.log('\n------------------------------------------------------------------------------');
  console.log('correctness');
  console.log('------------------------------------------------------------------------------');
  console.log(`  requests issued          ${args.keys * args.concurrency}`);
  console.log(`  charge rows written      ${total}   (must equal ${args.keys})`);
  console.log(`  KEYS CHARGED MORE THAN ONCE   ${dupKeys}`);
  console.log(`  DUPLICATE CHARGES (extra rows) ${extra}`);
  if (extra > 0) console.log('  ^ every one of these is a customer charged twice for one request.');

  console.log('\n------------------------------------------------------------------------------');
  console.log('what each request saw');
  console.log('------------------------------------------------------------------------------');
  const labels = { 201: '201 charged', 200: '200 replayed', 409: '409 conflict', 422: '422 fingerprint', 500: '500 error' };
  for (const code of [201, 200, 409, 422, 500]) {
    console.log(`  ${labels[code].padEnd(18)}${results.filter((r) => r.status === code).length}`);
  }
  const notes = new Map();
  for (const r of results) if (r.status >= 400) notes.set(r.note, (notes.get(r.note) || 0) + 1);
  for (const [note, n] of [...notes].sort((a, b) => b[1] - a[1]).slice(0, 5)) {
    console.log(`      ${String(n).padStart(5)}x  ${note.slice(0, 60)}`);
  }

  console.log('\n------------------------------------------------------------------------------');
  console.log('latency -- winners vs duplicates (the price of the design)');
  console.log('------------------------------------------------------------------------------');
  const winners = results.filter((r) => r.status === 201).map((r) => r.ms);
  const losers = results.filter((r) => r.status !== 201).map((r) => r.ms);
  console.log(`  ${''.padEnd(14)}${'p50'.padStart(12)}${'p99'.padStart(15)}${'max'.padStart(15)}`);
  for (const [name, v] of [['winner', winners], ['duplicate', losers]]) {
    if (v.length === 0) {
      console.log(`  ${name.padEnd(14)}${'-'.padStart(12)}${'-'.padStart(15)}${'-'.padStart(15)}`);
      continue;
    }
    console.log(`  ${name.padEnd(14)}${percentile(v, 0.5).toFixed(1).padStart(9)} ms` +
      `${percentile(v, 0.99).toFixed(1).padStart(12)} ms${Math.max(...v).toFixed(1).padStart(12)} ms`);
  }
  console.log(`\n  wall clock ${wall.toFixed(2)}s for ${args.keys * args.concurrency} requests`);
  console.log('\n  full breakdown:  psql -d sep_lab_04_dist -f sql/topic2_assert.sql');

  await admin.end();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
