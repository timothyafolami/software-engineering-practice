/**
 * Layer 5 - Topic 7: idempotency, and degradation decided in advance (Node.js).
 *
 * Runs the whole of the topic's experiment against a REAL local Postgres, with
 * no containers involved: the naive double-charge, the atomic
 * insert-on-unique-constraint that makes a retry safe, the ambiguous result
 * where the client never learns it succeeded, a crash between the claim and the
 * work, the fingerprint check, and a degradation matrix that is a table you
 * wrote in advance rather than an argument you have at 3am. Same scenarios,
 * same columns and the same conclusions as ../python/idempotency.py and
 * ../golang/idempotency.go.
 *
 * THE TWO NODE-SPECIFIC HAZARDS, BOTH REPRODUCED RATHER THAN DESCRIBED
 *
 * 1. TRANSPARENT STATEMENT REPLAY. Some clients and poolers re-execute a
 *    statement after a connection-level error, so one logical `INSERT` in your
 *    code can reach Postgres twice. Section 5 builds exactly such a wrapper --
 *    forty lines, and the kind of thing that gets added to a codebase under the
 *    name `withRetry` -- and then loses the response of a statement that
 *    already committed. Without a unique index the retry writes a second
 *    charge. With one, the same wrapper is harmless. That is the argument for
 *    putting the arbiter in the DATABASE: it has to live somewhere a
 *    transparent replay cannot bypass, and application logic is not such a
 *    place because the replay happens underneath it.
 *
 *    The connection loss in that section is INJECTED, deliberately and
 *    visibly, so the demonstration is deterministic. Racing a real
 *    `pg_terminate_backend` against a commit would show the same thing on
 *    perhaps one run in twenty.
 *
 * 2. THE ROLLBACK YOU DID NOT RUN. A pooled connection is not reset between
 *    borrowers -- no implicit `DISCARD ALL` -- so a client released while its
 *    transaction is still failed poisons the NEXT request, which belongs to
 *    somebody else, and the error it gets names a statement it never ran.
 *    Section 6 runs three variants on a pool of one: no rollback, an
 *    un-awaited rollback, and an awaited one, and prints what each actually
 *    did here.
 *
 * WHAT THIS DEMONSTRATES, IN ORDER
 *
 *   1. Setup      Two key tables: one with a UNIQUE index on `key`, one
 *                 without. Same SQL shape, one constraint apart.
 *   2. The race   50 concurrent requests released together by a barrier,
 *                 sharing ONE idempotency key, in five implementations.
 *   3. The ambiguous result: responses lost on the way back, clients retry.
 *   4. The crash test: dying between the claim and the work, then the TTL.
 *   5. Transparent statement replay, with and without the constraint.
 *   6. The rollback you did not run.
 *   7. The fingerprint test.
 *   8. Degradation decided in advance: the matrix, a kill switch flipped
 *      mid-run, and the row that is not a kill switch at all.
 *
 * WHAT TO LOOK FOR IN THE OUTPUT
 *
 *   - `charge_rows` in the naive concurrent row. Anything above 1 is money.
 *   - The `pool=1` row against the one above it: a SMALLER limit hiding the
 *     bug completely, which is the most dangerous kind of green test.
 *   - `409s` in claim+execute against single-txn. Both are correct; one makes
 *     the loser retry and the other makes it wait, and `loser_p99` prices the
 *     waiting.
 *   - `orphaned` in the crash rows, before and after the TTL expires.
 *   - Section 5's two row counts under the identical retry wrapper.
 *
 * REQUIREMENTS
 *     A local Postgres accepting connections (`pg_isready`). This program
 *     creates the `failure_lab` database if it is missing and owns the tables
 *     it makes inside it; `dropdb failure_lab` when you are done with the
 *     layer.
 *         npm install
 *
 * RUN
 *     node idempotency.js
 *
 * Takes about ten seconds. Takes no arguments.
 */
'use strict';

const crypto = require('node:crypto');
const { Pool, Client } = require('pg');

// ------------------------------------------------------------------ config

const DB_NAME = process.env.FAILURE_LAB_DB || 'failure_lab';
const CONCURRENCY = 50;        // the README's number: 50 requests sharing one key
const HOLD_MS = 25;            // widens the window; it does not create the race
const TTL_S = 60;
const CRASH_TTL_S = 2;
const AMBIGUOUS_KEYS = 20;
const AMBIGUOUS_LOSS_P = 0.5;
const MAX_CLIENT_ATTEMPTS = 5;

// An empty host means the libpq default: a local unix socket as the current
// user, which is what `psql` does and therefore what "a local Postgres" means.
const connOpts = (database) => ({ database });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function fingerprint(body) {
  const canonical = JSON.stringify(body, Object.keys(body).sort());
  return crypto.createHash('sha256').update(canonical).digest('hex');
}

/**
 * Re-serialise a JSON document with sorted keys.
 *
 * A replayed response comes back out of `jsonb`, which stores a parsed document
 * and prints it in its own key order -- so a byte comparison against the string
 * this process produced would differ for reasons that have nothing to do with
 * idempotency. `distinct_responses` counts answers, not encodings. (`jsonb` is
 * also why a stored response is not, strictly, replayed byte-for-byte: if that
 * matters to your API contract, store the body as `text`.)
 */
function canonical(v) {
  if (v === null || v === undefined) return '';
  const obj = typeof v === 'string' ? JSON.parse(v) : v;
  return JSON.stringify(obj, Object.keys(obj).sort());
}

// ------------------------------------------------------------------ schema

const SCHEMA_SQL = `
DROP TABLE IF EXISTS charges;
DROP TABLE IF EXISTS idempotency_keys;
DROP TABLE IF EXISTS idempotency_keys_naive;
DROP TABLE IF EXISTS replay_charges;

CREATE TABLE charges (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  idem_key      text NOT NULL,
  amount_cents  integer NOT NULL,
  currency      text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- The correct table. The PRIMARY KEY on the key column IS the unique index,
-- and that unique index is the entire mechanism -- not the SELECT, not the
-- application logic, and above all not the driver.
CREATE TABLE idempotency_keys (
  key         text PRIMARY KEY,
  fingerprint text NOT NULL,
  state       text NOT NULL,
  response    jsonb,
  claimed_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL
);

-- The naive table. The same columns, and no unique index on key. That single
-- difference is what the first three scenarios measure.
CREATE TABLE idempotency_keys_naive (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  key         text NOT NULL,
  fingerprint text NOT NULL,
  state       text NOT NULL,
  response    jsonb,
  claimed_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL
);

-- Section 5's table. Same shape twice over: the guarded column gets a unique
-- index, the unguarded one does not, and one retry wrapper writes to both.
CREATE TABLE replay_charges (
  id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  guarded  boolean NOT NULL,
  op_id    text NOT NULL
);
CREATE UNIQUE INDEX replay_charges_guarded_uniq ON replay_charges (op_id)
  WHERE guarded;
`;

async function ensureDatabase() {
  const admin = new Client(connOpts('postgres'));
  await admin.connect();
  try {
    const r = await admin.query('SELECT 1 FROM pg_database WHERE datname = $1', [DB_NAME]);
    if (r.rowCount === 0) {
      await admin.query(`CREATE DATABASE "${DB_NAME}"`);
      console.log(`  created database ${DB_NAME}`);
    }
  } finally {
    await admin.end();
  }
}

// ------------------------------------------------------------------ server

class Server {
  constructor(pool, { holdMs = HOLD_MS, ttlS = TTL_S } = {}) {
    this.pool = pool;
    this.holdMs = holdMs;
    this.ttlS = ttlS;
    this.crashAfterClaim = false;
  }

  /** The side effect. In real life a card is charged here. */
  async doWork(client, key, body) {
    await sleep(this.holdMs);
    const r = await client.query(
      'INSERT INTO charges (idem_key, amount_cents, currency) VALUES ($1,$2,$3) RETURNING id',
      [key, body.amount_cents, body.currency]);
    return {
      charge_id: Number(r.rows[0].id),
      amount_cents: body.amount_cents,
      currency: body.currency,
      status: 'succeeded',
    };
  }

  /**
   * Wrong at READ COMMITTED, and wrong in a way that reads as careful.
   *
   * Two concurrent transactions both SELECT, both see no row, and both proceed.
   * READ COMMITTED gives each statement a fresh snapshot of *committed* data,
   * and neither transaction has committed anything the other can see. The
   * unique index is the only thing that would have serialised them, and this
   * table does not have one.
   */
  async chargeNaive(key, body) {
    const fp = fingerprint(body);
    const client = await this.pool.connect();
    try {
      await client.query('BEGIN');
      const found = await client.query(
        'SELECT state, response FROM idempotency_keys_naive WHERE key = $1', [key]);
      if (found.rowCount > 0 && found.rows[0].state === 'completed') {
        await client.query('COMMIT');
        return { status: 200, body: canonical(found.rows[0].response), replayed: true };
      }
      if (found.rowCount === 0) {
        await client.query(
          `INSERT INTO idempotency_keys_naive (key, fingerprint, state, expires_at)
           VALUES ($1,$2,'in_progress', now() + make_interval(secs => $3))`,
          [key, fp, this.ttlS]);
      }
      const resp = await this.doWork(client, key, body);
      await client.query(
        "UPDATE idempotency_keys_naive SET state='completed', response=$2 WHERE key=$1",
        [key, JSON.stringify(resp)]);
      await client.query('COMMIT');
      return { status: 200, body: canonical(resp) };
    } catch (e) {
      await client.query('ROLLBACK').catch(() => {});   // awaited; see section 6
      throw e;
    } finally {
      client.release();
    }
  }

  /**
   * Claim, then execute. Two transactions, on purpose.
   *
   * The claim commits before the work starts, which is what lets a concurrent
   * request find `in_progress` and answer 409 immediately instead of holding a
   * connection open waiting for someone else's card charge. The price is the
   * crash window: if the executor dies between the two transactions, the claim
   * outlives it and blocks every retry until the TTL expires.
   *
   * The DO UPDATE arm is not a convenience: it is how a claim whose holder died
   * gets taken over, and it fires only for a stale in_progress row whose body
   * matches. A `completed` row can never satisfy the WHERE, so a finished key
   * is never re-executed no matter how late the retry arrives.
   */
  async chargeCorrect(key, body) {
    const fp = fingerprint(body);
    const claim = await this.pool.query(
      `INSERT INTO idempotency_keys (key, fingerprint, state, expires_at)
       VALUES ($1, $2, 'in_progress', now() + make_interval(secs => $3))
       ON CONFLICT (key) DO UPDATE
          SET state='in_progress', claimed_at=now(), expires_at=EXCLUDED.expires_at
        WHERE idempotency_keys.state='in_progress'
          AND idempotency_keys.expires_at < now()
          AND idempotency_keys.fingerprint = EXCLUDED.fingerprint
       RETURNING key`,
      [key, fp, this.ttlS]);

    if (claim.rowCount === 0) return this.replayOrConflict(key, fp);

    if (this.crashAfterClaim) {
      // The claim is committed and this process is about to stop existing.
      // Nothing rolls back, because there is no open transaction to roll back:
      // that is precisely why the row is now an orphan.
      throw new Error('simulated crash after claim, before work');
    }

    // The side effect and the stored response, in ONE transaction. If these
    // were two, a crash between them leaves a charge nobody can replay -- which
    // is worse than the orphan above, because the money moved.
    const client = await this.pool.connect();
    try {
      await client.query('BEGIN');
      const resp = await this.doWork(client, key, body);
      await client.query(
        "UPDATE idempotency_keys SET state='completed', response=$2 WHERE key=$1",
        [key, JSON.stringify(resp)]);
      await client.query('COMMIT');
      return { status: 200, body: canonical(resp) };
    } catch (e) {
      await client.query('ROLLBACK').catch(() => {});
      throw e;
    } finally {
      client.release();
    }
  }

  async replayOrConflict(key, fp) {
    const r = await this.pool.query(
      'SELECT state, fingerprint, response FROM idempotency_keys WHERE key=$1', [key]);
    if (r.rowCount === 0) return { status: 409 };
    const row = r.rows[0];
    if (row.fingerprint !== fp) return { status: 422 };
    if (row.state === 'completed') {
      return { status: 200, body: canonical(row.response), replayed: true };
    }
    return { status: 409 };
  }

  /**
   * Equally correct, and it never produces an orphan -- because there is no
   * window between the claim and the work for a crash to land in.
   *
   * What it produces instead is waiting. A loser's `INSERT ... ON CONFLICT`
   * blocks on the winner's uncommitted tuple until the winner commits, so every
   * concurrent duplicate holds a connection for the full duration of the work.
   * Read `loser_p99`: at a large enough duplicate rate that is a pool
   * exhaustion (topic 5) wearing an idempotency costume.
   */
  async chargeCorrectSingleTxn(key, body) {
    const fp = fingerprint(body);
    const client = await this.pool.connect();
    try {
      await client.query('BEGIN');
      const claim = await client.query(
        `INSERT INTO idempotency_keys (key, fingerprint, state, expires_at)
         VALUES ($1,$2,'in_progress', now() + make_interval(secs => $3))
         ON CONFLICT (key) DO NOTHING RETURNING key`, [key, fp, this.ttlS]);
      if (claim.rowCount > 0) {
        const resp = await this.doWork(client, key, body);
        await client.query(
          "UPDATE idempotency_keys SET state='completed', response=$2 WHERE key=$1",
          [key, JSON.stringify(resp)]);
        await client.query('COMMIT');
        return { status: 200, body: canonical(resp) };
      }
      // The INSERT above already waited for the winner to commit, so the row is
      // visible to this statement: READ COMMITTED takes a fresh snapshot per
      // statement, which is the same property that made the naive version wrong
      // and makes this one work.
      const r = await client.query(
        'SELECT state, fingerprint, response FROM idempotency_keys WHERE key=$1', [key]);
      await client.query('COMMIT');
      const row = r.rows[0];
      if (row.fingerprint !== fp) return { status: 422 };
      if (row.state === 'completed') {
        return { status: 200, body: canonical(row.response), replayed: true };
      }
      return { status: 409 };
    } catch (e) {
      await client.query('ROLLBACK').catch(() => {});
      throw e;
    } finally {
      client.release();
    }
  }
}

// ------------------------------------------------------------------ client

class Result {
  constructor() {
    this.statuses = [];
    this.latencies = [];
    this.bodies = new Map();
    this.errors = 0;
    this.attempts = 0;
  }

  record(status, latMs, body) {
    this.statuses.push(status);
    this.latencies.push(latMs);
    this.attempts += 1;
    if (body) this.bodies.set(body, (this.bodies.get(body) || 0) + 1);
  }
}

/**
 * Release `n` callers at the same instant.
 *
 * The gate is not decoration. Without it the calls ramp up, the first request
 * finishes before the last one starts, and the naive version passes -- which is
 * the top entry on the README's list of ways this experiment breaks rather than
 * the prediction being wrong.
 */
async function fireTogether(n, fn) {
  const res = new Result();
  let open;
  const gate = new Promise((r) => { open = r; });
  const runs = Array.from({ length: n }, (_, i) => (async () => {
    await gate;
    const t0 = process.hrtime.bigint();
    try {
      const r = await fn(i);
      res.record(r.status, Number(process.hrtime.bigint() - t0) / 1e6, r.body);
    } catch {
      res.errors += 1;
      res.attempts += 1;
    }
  })());
  open();
  await Promise.all(runs);
  return res;
}

// ------------------------------------------------------------------ counts

async function counts(pool, key) {
  const c = key
    ? await pool.query('SELECT count(*)::int AS n FROM charges WHERE idem_key=$1', [key])
    : await pool.query('SELECT count(*)::int AS n FROM charges');
  const o = await pool.query(
    "SELECT count(*)::int AS n FROM idempotency_keys WHERE state='in_progress' AND expires_at > now()");
  return { charges: c.rows[0].n, orphaned: o.rows[0].n };
}

function pct(vals, q) {
  if (!vals.length) return NaN;
  const s = [...vals].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.max(0, Math.ceil(q * s.length) - 1))];
}

function report(mode, res, c, extra = '') {
  const n409 = res.statuses.filter((s) => s === 409).length;
  const n422 = res.statuses.filter((s) => s === 422).length;
  console.log(`  mode=${mode.padEnd(32)} charge_rows=${String(c.charges).padEnd(4)} `
    + `distinct_responses=${String(res.bodies.size).padEnd(4)} `
    + `409s=${String(n409).padEnd(4)} 422s=${String(n422).padEnd(4)} `
    + `orphaned_in_progress=${String(c.orphaned).padEnd(4)} `
    + `attempts=${String(res.attempts).padEnd(4)}${extra}`);
}

function rule(title) {
  console.log();
  console.log('='.repeat(78));
  console.log(title);
  console.log('='.repeat(78));
}

// ------------------------------------------ section 5: transparent replay

/**
 * The wrapper. It looks defensive and it is forty lines of trouble.
 *
 * A connection-level error carries no information about whether the statement
 * committed -- that is the ambiguous result again, one layer down -- so
 * re-executing is a guess. The guess is safe exactly when the statement is
 * idempotent, and nothing in this function can know whether it is.
 */
async function withRetry(pool, sql, params, lossPlan) {
  for (let attempt = 1; ; attempt += 1) {
    const r = await pool.query(sql, params);
    if (lossPlan.attemptsToLose >= attempt) {
      // INJECTED: the statement committed, and the client is told the
      // connection died before it could read the answer. Deterministic here;
      // in production it is a TCP reset, a pooler failover, or a `pg_bouncer`
      // server-side disconnect, and it is rare enough to be invisible in
      // testing and frequent enough to matter at scale.
      lossPlan.injected += 1;
      continue;
    }
    return r;
  }
}

async function transparentReplay(pool) {
  const opId = `op-${Date.now()}`;
  console.log('  One logical INSERT per column, run through the SAME retry wrapper.');
  console.log('  The wrapper loses the response of the first execution in both cases.');
  console.log();

  const unguarded = { attemptsToLose: 1, injected: 0 };
  await withRetry(pool,
    'INSERT INTO replay_charges (guarded, op_id) VALUES (false, $1)', [opId], unguarded);

  const guarded = { attemptsToLose: 1, injected: 0 };
  await withRetry(pool,
    `INSERT INTO replay_charges (guarded, op_id) VALUES (true, $1)
     ON CONFLICT (op_id) WHERE guarded DO NOTHING`, [opId], guarded);

  const r = await pool.query(
    `SELECT guarded, count(*)::int AS n FROM replay_charges
      WHERE op_id=$1 GROUP BY guarded ORDER BY guarded`, [opId]);
  for (const row of r.rows) {
    const label = row.guarded ? 'unique index + ON CONFLICT' : 'no unique index';
    const verdict = row.guarded ? (row.n === 1 ? 'PASS' : 'FAIL') : 'this is the bug';
    console.log(`    ${label.padEnd(28)} rows written = ${row.n}   [${verdict}]`);
  }
  console.log();
  console.log('  Identical application code, identical retry wrapper, identical injected');
  console.log('  loss. The only difference is whether an arbiter exists somewhere the');
  console.log('  replay cannot bypass -- and application logic is never such a place,');
  console.log('  because the replay happens underneath it.');
}

// --------------------------------- section 6: the rollback you did not run

/**
 * A pooled connection is not reset between borrowers.
 *
 * node-postgres hands the next caller the same socket, in whatever state the
 * previous caller left it: no implicit `DISCARD ALL`, no rollback, nothing. So
 * a handler that returns a client while its transaction is still open or
 * already failed poisons the NEXT request, which is a different request, on a
 * different endpoint, belonging to a different user -- and the error message it
 * gets is about a statement it never ran.
 *
 * Three variants, on a pool of one so the next borrower is necessarily the
 * connection the previous borrower returned. Each prints what actually
 * happened here rather than what folklore says happens.
 */
async function rollbackHygiene() {
  const pool = new Pool({ ...connOpts(DB_NAME), max: 1 });
  const attempt = async (mode) => {
    const c = await pool.connect();
    try {
      await c.query('BEGIN');
      await c.query('SELECT 1 FROM no_such_table_here');
    } catch {
      if (mode === 'await') await c.query('ROLLBACK');
      else if (mode === 'no-await') c.query('ROLLBACK').catch(() => {});
      // mode === 'none': the catch block logs and moves on, which is the
      // shape of the code that causes this.
    } finally {
      c.release();
    }
    try {
      const r = await pool.query('SELECT 42 AS answer');
      return `next borrower got ${r.rows[0].answer}`;
    } catch (e) {
      return `next borrower FAILED: ${String(e.message).slice(0, 74)}`;
    }
  };

  console.log('  (a) no rollback at all, client released from the catch block:');
  console.log(`      ${await attempt('none')}`);
  console.log('  (b) rollback issued but NOT awaited:');
  console.log(`      ${await attempt('no-await')}`);
  console.log('  (c) rollback awaited:');
  console.log(`      ${await attempt('await')}`);
  console.log();
  console.log('  (a) is the bug, and it is the same bug as Python\'s poisoned Session one');
  console.log('  directory over: the connection is still inside a failed transaction and');
  console.log('  Postgres refuses everything until it ends. The victim is the next');
  console.log('  request, not this one, which is why it is diagnosed slowly.');
  console.log();
  console.log('  (b) survives HERE because node-postgres queues queries per client, so');
  console.log('  the un-awaited ROLLBACK is still ahead of the next borrower\'s query in');
  console.log('  that connection\'s own queue. That is a property of this driver, not a');
  console.log('  license: the promise is unhandled, so its failure would be invisible,');
  console.log('  and any pooler that does not preserve per-connection ordering turns (b)');
  console.log('  straight back into (a). node-postgres also prints a DeprecationWarning');
  console.log('  above for exactly this shape -- queueing on a client that is already');
  console.log('  executing, removed in pg@9 -- so the property (b) leans on is going');
  console.log('  away on a published schedule. Await it.');
  await pool.end();
}

// ------------------------------------------- degradation, decided in advance

const DEGRADATION_MATRIX = [
  [0, 'authorise + capture', 'nothing works; this is the product', 'none - never shed', 'nobody', 'total'],
  [1, '3-D Secure step-up', 'non-authenticated auth; issuer may decline more', 'flag: risk.stepup', 'on-call', 'higher decline rate'],
  [2, 'fraud enrichment', 'cached features only; wider manual review queue', 'flag: risk.enrich', 'on-call', 'review backlog'],
  [2, 'currency rate refresh', 'last-known rate, capped staleness', 'config: fx.freeze', 'on-call', 'small FX drift'],
  [3, 'receipt email', 'queued, sent late; nothing is lost', 'flag: notify.receipt', 'on-call', 'support tickets'],
  [3, 'analytics fan-out', 'dashboards go stale for the duration', 'flag: analytics.emit', 'on-call', 'reporting only'],
];

/** Read at request time, never at module load. That is the whole design. */
class Flags {
  constructor() {
    this.v = { 'risk.enrich': true, 'notify.receipt': true, 'analytics.emit': true };
  }
  get(name) { return this.v[name] !== false; }
  set(name, value) { this.v[name] = value; }
}

// A flag read once, at module load, into a constant. It is in the matrix and it
// has an owner and it looks exactly like the others. It is not a kill switch:
// flipping it changes nothing until someone deploys.
const BAKED_IN_ENRICH_ENABLED = true;

async function degradationDemo(flags) {
  let depUp = false;

  const handle = async () => {
    if (flags.get('risk.enrich')) {
      await sleep(60);                       // the sick dependency
      if (!depUp) return false;              // tier 2 failing takes tier 0 down
    }
    if (flags.get('notify.receipt')) await sleep(2);
    return true;
  };
  const measure = async (n = 60) => {
    const t0 = Date.now();
    let ok = 0;
    for (let i = 0; i < n; i += 1) if (await handle()) ok += 1;
    const el = (Date.now() - t0) / 1000;
    return [(100.0 * ok) / n, n / el];
  };

  console.log('  The matrix, written before the incident:');
  console.log(`    ${'tier'.padEnd(5)}${'feature'.padEnd(24)}${'off looks like'.padEnd(50)}`
    + `${'kill switch'.padEnd(22)}blast radius`);
  for (const [tier, feature, off, sw, , blast] of [...DEGRADATION_MATRIX].sort((a, b) => a[0] - b[0])) {
    console.log(`    ${String(tier).padEnd(5)}${feature.padEnd(24)}${off.padEnd(50)}${sw.padEnd(22)}${blast}`);
  }
  console.log();
  console.log('  Shed order follows the tier column, which is business importance --');
  console.log('  not code structure, and not whatever is easiest to switch off.');
  console.log();

  let [ok, rps] = await measure();
  console.log(`  dependency down, matrix not applied:  success=${ok.toFixed(1).padStart(5)}%  goodput=${rps.toFixed(1).padStart(6)}/s`);
  flags.set('risk.enrich', false);      // tier 2 first
  flags.set('analytics.emit', false);   // tier 3
  [ok, rps] = await measure();
  console.log('  after flipping risk.enrich + analytics.emit (no deploy, no restart):');
  console.log(`                                        success=${ok.toFixed(1).padStart(5)}%  goodput=${rps.toFixed(1).padStart(6)}/s`);
  console.log();
  console.log(`  BAKED_IN_ENRICH_ENABLED is still ${BAKED_IN_ENRICH_ENABLED}. It is in the matrix, it has an`);
  console.log('  owner, and it cannot be changed without a deploy -- so it is not a kill');
  console.log('  switch. Any row like it is a plan, not a control.');
  depUp = true;
  return depUp;
}

// -------------------------------------------------------------------- main

async function main() {
  rule('Layer 5 - Topic 7: idempotency and degradation, decided in advance (Node.js)');
  await ensureDatabase();
  const pool = new Pool({ ...connOpts(DB_NAME), max: CONCURRENCY });
  await pool.query(SCHEMA_SQL);
  console.log(`  database          ${DB_NAME} (local, no containers)`);
  console.log(`  concurrency       ${CONCURRENCY} requests sharing ONE idempotency key`);
  console.log(`  work window       ${HOLD_MS} ms inside the executing transaction`);
  console.log("  isolation         READ COMMITTED (Postgres' default; not changed anywhere)");
  console.log('  driver            node-postgres 8.x, pool of ' + CONCURRENCY);

  const body = { amount_cents: 4200, currency: 'usd' };
  const srv = new Server(pool);

  // ---------------------------------------------------------- scenarios
  rule('THE RACE: 50 requests, one key, released together');

  let key = `naive-seq-${Date.now()}`;
  let res = new Result();
  for (let i = 0; i < CONCURRENCY; i += 1) {
    const t0 = process.hrtime.bigint();
    const r = await srv.chargeNaive(key, body);
    res.record(r.status, Number(process.hrtime.bigint() - t0) / 1e6, r.body);
  }
  report('naive / sequential', res, await counts(pool, key), '   <- correct, and it proves nothing');

  key = `naive-conc-${Date.now()}`;
  res = await fireTogether(CONCURRENCY, () => srv.chargeNaive(key, body));
  report('naive / 50 concurrent', res, await counts(pool, key), '   <- every row is a charge to a real card');

  const small = new Pool({ ...connOpts(DB_NAME), max: 1 });
  const srvSmall = new Server(small);
  key = `naive-pool1-${Date.now()}`;
  res = await fireTogether(CONCURRENCY, () => srvSmall.chargeNaive(key, body));
  report('naive / 50 concurrent / pool=1', res, await counts(pool, key), '   <- same bug, hidden by a SMALLER limit');
  await small.end();

  key = `correct-${Date.now()}`;
  res = await fireTogether(CONCURRENCY, () => srv.chargeCorrect(key, body));
  let lp = pct(res.latencies.filter((_, i) => res.statuses[i] !== 200), 0.99);
  report('correct / claim + execute', res, await counts(pool, key),
    `   loser_p99=${lp.toFixed(1).padStart(6)}ms`);

  key = `correct1txn-${Date.now()}`;
  res = await fireTogether(CONCURRENCY, () => srv.chargeCorrectSingleTxn(key, body));
  const winners = res.latencies.filter((_, i) => res.statuses[i] === 200).sort((a, b) => a - b);
  lp = pct(winners.slice(1), 0.99);      // drop the winner; the rest waited
  report('correct / single transaction', res, await counts(pool, key),
    `   loser_p99=${lp.toFixed(1).padStart(6)}ms  <- they waited instead of 409ing`);

  // ------------------------------------------------- the ambiguous result
  rule('THE AMBIGUOUS RESULT: the server succeeded, the client never heard');
  console.log(`  Each client retries its own key up to ${MAX_CLIENT_ATTEMPTS} times; every response has a `
    + `${(AMBIGUOUS_LOSS_P * 100).toFixed(0)}% chance of being lost on the way back.`);
  console.log("  The client cannot tell 'did not happen' from 'happened, answer lost'.");
  console.log('  That is not a bug to fix; it is the situation. Idempotency is what');
  console.log('  makes the only available action -- retry -- safe.');
  console.log();
  const before = (await counts(pool, null)).charges;
  const ambKeys = Array.from({ length: AMBIGUOUS_KEYS }, (_, i) => `amb-${i}-${Date.now()}`);
  let attempts = 0;
  let seed = 20260819;
  const rand = () => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648; };
  res = await fireTogether(AMBIGUOUS_KEYS, async (i) => {
    for (let a = 0; a < MAX_CLIENT_ATTEMPTS; a += 1) {
      attempts += 1;
      const r = await srv.chargeCorrect(ambKeys[i], body);
      if (r.status === 409) { await sleep(20); continue; }
      if (rand() < AMBIGUOUS_LOSS_P) continue;   // response lost; charge happened
      return r;
    }
    return { status: 504 };
  });
  res.attempts = attempts;
  const after = await counts(pool, null);
  const delta = after.charges - before;
  report('correct + retries + lost responses', res, { charges: delta, orphaned: after.orphaned },
    `   distinct keys=${AMBIGUOUS_KEYS}  [${delta === AMBIGUOUS_KEYS ? 'PASS' : 'FAIL'}]`);

  // ------------------------------------------------------- the crash test
  rule('THE CRASH TEST: dying between the claim and the work');
  const crashSrv = new Server(pool, { ttlS: CRASH_TTL_S });
  key = `crash-${Date.now()}`;
  crashSrv.crashAfterClaim = true;
  try {
    await crashSrv.chargeCorrect(key, body);
  } catch (e) {
    console.log(`  executor died: ${e.message}`);
  }
  crashSrv.crashAfterClaim = false;
  res = await fireTogether(5, () => crashSrv.chargeCorrect(key, body));
  report('correct + crash, TTL not yet expired', res, await counts(pool, key),
    "   <- every retry blocked by a dead holder's claim");
  console.log(`  the claim's TTL is ${CRASH_TTL_S}s. Waiting it out...`);
  await sleep(CRASH_TTL_S * 1000 + 300);
  res = await fireTogether(5, () => crashSrv.chargeCorrect(key, body));
  report('correct + crash, after TTL expiry', res, await counts(pool, key),
    '   <- reclaimed by ON CONFLICT DO UPDATE, still one charge');
  console.log();
  console.log('  The TTL is the only thing that unblocks a claim whose holder is gone, so');
  console.log('  the client\'s retry window must be SHORTER than the retention, or the');
  console.log('  guarantee evaporates at exactly the moment it is needed.');

  // ------------------------------------------------- transparent replay
  rule('TRANSPARENT STATEMENT REPLAY (the node-postgres-shaped hazard)');
  await transparentReplay(pool);

  // ------------------------------------------------ the un-awaited rollback
  rule('THE ROLLBACK YOU DID NOT RUN');
  await rollbackHygiene();

  // -------------------------------------------------------- fingerprints
  rule('THE FINGERPRINT TEST: same key, different body');
  key = `fp-${Date.now()}`;
  const first = await srv.chargeCorrect(key, body);
  const other = { amount_cents: 99900, currency: 'usd' };
  const second = await srv.chargeCorrect(key, other);
  const c = await counts(pool, key);
  console.log(`  first  request  amount=${String(body.amount_cents).padStart(7)}  -> ${first.status}  ${first.body}`);
  console.log(`  second request  amount=${String(other.amount_cents).padStart(7)}  -> ${second.status}  ${second.body || '(no body)'}`);
  console.log(`  charge rows for this key: ${c.charges}   `
    + `[${second.status === 422 && c.charges === 1 ? 'PASS' : 'FAIL'}]`);
  console.log('  Without the fingerprint the second request replays the FIRST answer, so');
  console.log('  a client that reused a key by accident is told its $999 charge succeeded.');

  // ------------------------------------------------------- degradation
  rule('DEGRADATION DECIDED IN ADVANCE');
  await degradationDemo(new Flags());

  await pool.end();
  console.log();
}

main().catch((e) => { console.error(e); process.exit(1); });
