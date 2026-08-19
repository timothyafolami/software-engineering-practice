// Layer 4 Topic 6 -- the idempotent consumer, at the other end of the pipeline.
//
// WHAT THIS DEMONSTRATES: an at-least-once delivery stream (the outbox relay
// WILL redeliver -- that is the deal you accepted in exchange for never losing a
// message) consumed two ways, keyed on the outbox row id:
//
//   --mode check-then-act   SELECT to see whether this id was handled, and if
//                           not, handle it. Reads perfectly naturally in
//                           JavaScript, which is exactly why it is here, and it
//                           duplicates the EFFECT under concurrency for the same
//                           reason Topic 2's implementation A did.
//   --mode insert-then-act  INSERT ... ON CONFLICT DO NOTHING on a unique
//                           (run_id, outbox_id), and act only if rowCount === 1.
//                           The database decides who handles it, in the same
//                           transaction as the effect.
//
// WHAT TO LOOK FOR IN THE OUTPUT: the DUPLICATE EFFECTS count. Duplicate
// DELIVERIES are expected and are not a bug -- they are the whole point of
// at-least-once. A duplicate effect is the bug, and the fix is Topic 2's, not
// this topic's.
//
//   npm install                              # once, in this directory
//   node idempotent_consumer.js --mode check-then-act  --consumers 4
//   node idempotent_consumer.js --mode insert-then-act --consumers 4
//   psql -d sep_lab_04_dist -f sql/topic6_reconcile.sql
//
// Run a writer and a relay first, so there is something to consume:
//   python3 python/hwm_skip.py --writers 3 --hold-seconds 1 --duration 30 &
//   node nodejs/idempotent_consumer.js --mode check-then-act --consumers 4

'use strict';

const { Client } = require('pg');

const DSN = process.env.LAB_DSN || 'postgresql:///sep_lab_04_dist';

// Table names carry a t6_ prefix: the whole layer shares one scratch database
// (see ../../lab/README.md) and Topic 2 already owns a table called `charges`.
const DDL = `
CREATE TABLE IF NOT EXISTS t6_consumer_effects (
    id           bigserial PRIMARY KEY,
    run_id       text        NOT NULL,
    mode         text        NOT NULL,
    outbox_id    bigint      NOT NULL,
    consumer     text        NOT NULL,
    applied_at   timestamptz NOT NULL DEFAULT clock_timestamp()
    -- NO unique constraint on (run_id, outbox_id) at the table level, on
    -- purpose: with one, check-then-act would be rescued by the database and the
    -- comparison would show nothing. The unique index that DOES the work lives
    -- on t6_consumer_claims below, which is the claim, not the effect.
);

CREATE TABLE IF NOT EXISTS t6_consumer_claims (
    run_id     text        NOT NULL,
    outbox_id  bigint      NOT NULL,
    consumer   text        NOT NULL,
    claimed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, outbox_id)
);`;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function parseArgs(argv) {
  const args = { mode: 'insert-then-act', consumers: 4, seconds: 20, batch: 25, holdMs: 5 };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--mode') args.mode = argv[++i];
    else if (a === '--consumers') args.consumers = Number(argv[++i]);
    else if (a === '--seconds') args.seconds = Number(argv[++i]);
    else if (a === '--batch') args.batch = Number(argv[++i]);
    else if (a === '--hold-ms') args.holdMs = Number(argv[++i]);
    else { console.error(`unknown argument: ${a}`); process.exit(2); }
  }
  if (!['check-then-act', 'insert-then-act'].includes(args.mode)) {
    console.error('usage: node idempotent_consumer.js --mode check-then-act|insert-then-act ' +
      '[--consumers N] [--seconds N] [--batch N] [--hold-ms N]');
    process.exit(2);
  }
  return args;
}

/** THE BUG. Between the SELECT and the INSERT, another consumer does the same
 *  thing. `await` did not serialise anything -- it yielded this connection's
 *  turn, not everybody else's. */
async function checkThenAct(c, runId, outboxId, consumer, holdMs) {
  const seen = await c.query(
    'SELECT 1 FROM t6_consumer_claims WHERE run_id = $1 AND outbox_id = $2',
    [runId, outboxId]
  );
  if (seen.rowCount > 0) return false;
  if (holdMs) await sleep(holdMs);      // the effect takes a moment: an API call
  await c.query(
    `INSERT INTO t6_consumer_effects (run_id, mode, outbox_id, consumer)
     VALUES ($1, 'check-then-act', $2, $3)`,
    [runId, outboxId, consumer]
  );
  await c.query(
    `INSERT INTO t6_consumer_claims (run_id, outbox_id, consumer)
     VALUES ($1, $2, $3) ON CONFLICT DO NOTHING`,
    [runId, outboxId, consumer]
  );
  return true;
}

/** THE FIX. The claim and the effect commit together, and "did I win?" is
 *  rowCount === 1 rather than something this process decided. */
async function insertThenAct(c, runId, outboxId, consumer, holdMs) {
  await c.query('BEGIN');
  try {
    const claim = await c.query(
      `INSERT INTO t6_consumer_claims (run_id, outbox_id, consumer)
       VALUES ($1, $2, $3) ON CONFLICT (run_id, outbox_id) DO NOTHING`,
      [runId, outboxId, consumer]
    );
    if (claim.rowCount !== 1) {
      await c.query('COMMIT');
      return false;                     // somebody else owns this message
    }
    if (holdMs) await sleep(holdMs);
    await c.query(
      `INSERT INTO t6_consumer_effects (run_id, mode, outbox_id, consumer)
       VALUES ($1, 'insert-then-act', $2, $3)`,
      [runId, outboxId, consumer]
    );
    await c.query('COMMIT');
    return true;
  } catch (err) {
    await c.query('ROLLBACK');
    throw err;
  }
}

async function consumer(name, args, deadline, counters) {
  const c = new Client({ connectionString: DSN, application_name: 'sep-layer4-lab' });
  await c.connect();
  const handler = args.mode === 'check-then-act' ? checkThenAct : insertThenAct;
  while (Date.now() < deadline) {
    // At-least-once: every consumer reads the SAME deliveries. That is not a
    // harness shortcut -- it is what a consumer group looks like when a rebalance
    // replays a partition, or when the relay redelivers after a crash mid-batch.
    const { rows } = await c.query(
      `SELECT outbox_id, run_id FROM t6_delivered
        ORDER BY id DESC LIMIT $1`, [args.batch]
    );
    for (const row of rows) {
      if (Date.now() >= deadline) break;
      try {
        const applied = await handler(c, row.run_id, row.outbox_id, name, args.holdMs);
        counters.attempts++;
        if (applied) counters.applied++;
      } catch (err) {
        counters.errors++;
        counters.lastError = err.code ? `${err.code} ${err.message}` : err.message;
      }
    }
    await sleep(50);
  }
  await c.end();
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
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

  const pending = await admin.query('SELECT count(*)::int AS n FROM t6_delivered');
  if (pending.rows[0].n === 0) {
    console.error('t6_delivered is empty -- there is nothing to consume.\n' +
      'Run a writer and a relay first:\n' +
      '  python3 python/hwm_skip.py --writers 3 --hold-seconds 1 --duration 30');
    process.exit(1);
  }
  // Start from a clean sheet for THIS mode only, so the counts below describe
  // this run rather than every run since the database was created.
  await admin.query('DELETE FROM t6_consumer_effects WHERE mode = $1', [args.mode]);
  await admin.query(
    `DELETE FROM t6_consumer_claims c
      USING t6_delivered d WHERE d.outbox_id = c.outbox_id AND d.run_id = c.run_id`);

  console.log('\n==============================================================================');
  console.log(`Topic 6 -- idempotent consumer, mode ${args.mode}`);
  console.log('==============================================================================');
  console.log(`  consumers     : ${args.consumers}, all reading the SAME deliveries`);
  console.log(`  effect hold   : ${args.holdMs} ms inside the handler`);
  console.log(`  deliveries    : ${pending.rows[0].n} rows in t6_delivered`);
  console.log('  effects table : NO unique constraint -- deliberate, see the DDL comment');

  const deadline = Date.now() + args.seconds * 1000;
  const counters = { attempts: 0, applied: 0, errors: 0, lastError: null };
  await Promise.all(
    Array.from({ length: args.consumers }, (_, i) => consumer(`c${i}`, args, deadline, counters))
  );

  const q = async (sql) => (await admin.query(sql, [args.mode])).rows[0];
  const effects = (await q('SELECT count(*)::int AS n FROM t6_consumer_effects WHERE mode = $1')).n;
  const distinct = (await q(
    'SELECT count(DISTINCT (run_id, outbox_id))::int AS n FROM t6_consumer_effects WHERE mode = $1')).n;

  console.log('\n------------------------------------------------------------------------------');
  console.log('the only count that may not duplicate');
  console.log('------------------------------------------------------------------------------');
  console.log(`  handler invocations        ${counters.attempts}`);
  console.log(`  claimed and acted          ${counters.applied}`);
  console.log(`  effect rows written        ${effects}`);
  console.log(`  distinct messages          ${distinct}`);
  console.log(`  DUPLICATE EFFECTS          ${effects - distinct}`);
  if (counters.errors) {
    console.log(`  driver errors              ${counters.errors}  (last: ${counters.lastError})`);
  }
  console.log();
  if (effects - distinct > 0) {
    console.log('  Every one of these is the same message acted on twice: a second');
    console.log('  refund issued, a second email sent, a second row in a ledger. The');
    console.log('  delivery layer did nothing wrong -- at-least-once promised exactly');
    console.log('  this, and the consumer was supposed to be ready for it.');
  } else {
    console.log('  Duplicate DELIVERIES still happened -- every consumer read every row.');
    console.log('  They produced no duplicate EFFECT, which is the whole distinction:');
    console.log('  you do not stop the redelivery, you make it harmless.');
  }
  console.log('\n  full breakdown:  psql -d sep_lab_04_dist -f sql/topic6_reconcile.sql');
  await admin.end();
}

main().catch((err) => { console.error(err); process.exit(1); });
