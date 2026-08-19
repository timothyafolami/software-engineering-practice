// Layer 3, Topic 6 - N+1 without an ORM, and the batching window that fixes it.
//
// WHAT IT DEMONSTRATES: `pg` has no lazy loading to blame, and the N+1 turns up
// anyway, because GraphQL-style resolvers make it the DEFAULT. A resolver runs
// once per field per object, so `orders { customer { email } }` over 200 orders
// calls the customer resolver 200 times -- by design, correctly, one query each.
//
// DataLoader is the fix, and its mechanism is the whole lesson: it collects
// every key requested WITHIN ONE TICK OF THE EVENT LOOP, then issues a single
// query with `WHERE id = ANY($1)`. That is the same `IN (...)` idea as
// SQLAlchemy's selectinload, moved out of the ORM and into the application --
// and it works because Node's single-threaded event loop gives you a natural,
// well-defined batching window. Layer 1's concurrency model showing up as a
// data-access strategy.
//
// The loader here is hand-written, about thirty lines, rather than `npm install
// dataloader`. The library is excellent and you should use it; writing it once
// is what makes the failure mode below obvious instead of mysterious.
//
// WHAT TO LOOK FOR: three rows.
//   resolver per field    -- N+1, one query per order
//   DataLoader, same tick -- 2 queries, whatever N is
//   DataLoader, across ticks -- N+1 again, with a DataLoader that is present,
//                            correctly configured, and doing nothing. One
//                            `await` between the requests ends the batching
//                            window, and nothing anywhere reports that.
//
// The third row is why this program exists. A batching loader that is not
// batching looks exactly like one that is, in code, in config, and in every
// dashboard except the query count.
//
// Run:  node 06-finding-n-plus-1/nodejs/dataloader_batching.js
// DSN:  LAB_PG_URL, default postgres:///sep_lab_03_data?host=/tmp
// Dep:  npm install --prefix 06-finding-n-plus-1/nodejs

'use strict';

let Client;
try {
  ({ Client } = require('pg'));
} catch (err) {
  console.error('This program needs node-postgres.');
  console.error('  unblock: npm install --prefix 06-finding-n-plus-1/nodejs');
  process.exit(1);
}

const DSN = process.env.LAB_PG_URL || 'postgres:///sep_lab_03_data?host=/tmp';
const LIMITS = (process.env.LIMITS || '10,100,500').split(',').map(Number);
const REPEATS = Number(process.env.REPEATS || 5);

// ---------------------------------------------------------------------------
// A counting wrapper around the client. Same idea as the Python side's
// before_cursor_execute hook: count at the driver, not in application code, so
// it sees statements nobody wrote a call for.
// ---------------------------------------------------------------------------
class CountingClient {
  constructor(client) {
    this.client = client;
    this.queries = 0;
    this.rows = 0;
    this.counting = false;
  }

  async query(text, values) {
    const res = await this.client.query(text, values);
    if (this.counting) {
      this.queries += 1;
      this.rows += res.rowCount || 0;
    }
    return res;
  }

  begin() { this.counting = true; this.queries = 0; this.rows = 0; }

  end() { this.counting = false; return { queries: this.queries, rows: this.rows }; }
}

// ---------------------------------------------------------------------------
// The loader. Thirty lines, and every one of them is the mechanism.
// ---------------------------------------------------------------------------
class BatchLoader {
  constructor(batchFn) {
    this.batchFn = batchFn;
    this.queue = [];      // keys requested since the last flush
    this.scheduled = false;
  }

  load(key) {
    return new Promise((resolve, reject) => {
      this.queue.push({ key, resolve, reject });
      if (!this.scheduled) {
        this.scheduled = true;
        // THE BATCHING WINDOW, and the entire subtlety of this pattern.
        // process.nextTick fires after the current synchronous run of JS
        // finishes and BEFORE the event loop moves on -- so every load() called
        // in this tick lands in the same batch. Any `await` on real I/O between
        // two load() calls ends the tick, the flush happens, and the next
        // load() starts a batch of one.
        process.nextTick(() => this.flush());
      }
    });
  }

  async flush() {
    const batch = this.queue;
    this.queue = [];
    this.scheduled = false;
    if (batch.length === 0) return;
    try {
      const keys = batch.map((b) => b.key);
      const byKey = await this.batchFn(keys);
      for (const b of batch) b.resolve(byKey.get(b.key));
    } catch (err) {
      for (const b of batch) b.reject(err);
    }
  }
}

function customerLoader(db) {
  return new BatchLoader(async (ids) => {
    // ONE query for the whole batch. `= ANY($1)` rather than a built-up
    // `IN (...)` string: one bound array parameter, one entry in
    // pg_stat_statements, and nothing interpolated into SQL.
    const { rows } = await db.query(
      'SELECT id, email FROM customers WHERE id = ANY($1)', [[...new Set(ids)]]);
    return new Map(rows.map((r) => [r.id, r]));
  });
}

// ---------------------------------------------------------------------------
// The three variants.
// ---------------------------------------------------------------------------

async function resolverPerField(db, limit) {
  const { rows: orders } = await db.query(
    'SELECT id, customer_id, status FROM orders ORDER BY id LIMIT $1', [limit]);
  // This is what a GraphQL runtime does for you: call the field resolver once
  // per parent object. Written out, it is obviously N queries. Inside a schema
  // it is one line of SDL and looks like nothing at all.
  //
  // Sequential here because this program shares one connection. A real GraphQL
  // server fans these out across a POOL, which runs them concurrently and hides
  // some of the latency -- it does not reduce the query count by one, and it
  // turns an N+1 into pool pressure instead, which is Topic 7's whole subject.
  const out = [];
  for (const o of orders) {
    const { rows } = await db.query('SELECT id, email FROM customers WHERE id = $1',
      [o.customer_id]);
    out.push({ id: o.id, status: o.status, email: rows[0].email });
  }
  return out;
}

async function dataloaderSameTick(db, limit) {
  const loader = customerLoader(db);
  const { rows: orders } = await db.query(
    'SELECT id, customer_id, status FROM orders ORDER BY id LIMIT $1', [limit]);
  // Every load() call happens in this one synchronous map, so every key lands
  // in one batch. The awaits all happen afterwards, on Promises already queued.
  const pending = orders.map((o) => loader.load(o.customer_id));
  const customers = await Promise.all(pending);
  return orders.map((o, i) => ({ id: o.id, status: o.status, email: customers[i].email }));
}

async function dataloaderAcrossTicks(db, limit) {
  const loader = customerLoader(db);
  const { rows: orders } = await db.query(
    'SELECT id, customer_id, status FROM orders ORDER BY id LIMIT $1', [limit]);
  const out = [];
  for (const o of orders) {
    // The one-character difference: awaiting each load before requesting the
    // next. The loader flushes a batch of exactly one, every time. The
    // configuration is identical to the row above and the behaviour is
    // identical to the row above THAT.
    const c = await loader.load(o.customer_id);
    out.push({ id: o.id, status: o.status, email: c.email });
  }
  return out;
}

const VARIANTS = [
  ['resolver per field', resolverPerField],
  ['DataLoader, same tick', dataloaderSameTick],
  ['DataLoader, across ticks', dataloaderAcrossTicks],
];

function percentile(values, q) {
  if (!values.length) return NaN;
  const s = [...values].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.max(0, Math.round((q / 100) * s.length + 0.5) - 1))];
}

async function measure(db, fn, limit) {
  const latencies = [];
  let counts = { queries: 0, rows: 0 };
  for (let i = 0; i < REPEATS; i += 1) {
    db.begin();
    const t0 = process.hrtime.bigint();
    await fn(db, limit);
    latencies.push(Number(process.hrtime.bigint() - t0) / 1e6);
    counts = db.end();
  }
  return { ...counts, p50: percentile(latencies, 50), p99: percentile(latencies, 99) };
}

async function main() {
  const client = new Client({ connectionString: DSN });
  try {
    await client.connect();
  } catch (err) {
    console.error(`could not connect to ${DSN}: ${err.message}`);
    console.error('  unblock: python3 lab/local/check_env.py');
    process.exit(1);
  }
  const db = new CountingClient(client);

  console.log('='.repeat(78));
  console.log('N+1 without an ORM: resolvers, DataLoader, and the batching window');
  console.log('='.repeat(78));
  const { rows: [v] } = await client.query('SELECT version()');
  console.log(v.version.split(' on ')[0]);
  console.log('\nThe endpoint: N orders, each with its customer email. Three ways to get it.\n');

  console.log(`  ${'variant'.padEnd(26)}${'limit'.padStart(7)}${'queries'.padStart(10)}`
    + `${'rows'.padStart(9)}${'p50 ms'.padStart(10)}${'p99 ms'.padStart(10)}`);
  console.log(`  ${'-'.repeat(70)}`);
  const results = {};
  for (const limit of LIMITS) {
    for (const [label, fn] of VARIANTS) {
      const r = await measure(db, fn, limit);
      results[`${label}|${limit}`] = r;
      console.log(`  ${label.padEnd(26)}${String(limit).padStart(7)}`
        + `${String(r.queries).padStart(10)}${String(r.rows).padStart(9)}`
        + `${r.p50.toFixed(1).padStart(10)}${r.p99.toFixed(1).padStart(10)}`);
    }
    console.log('');
  }

  const big = LIMITS[LIMITS.length - 1];
  const naive = results[`resolver per field|${big}`];
  const batched = results[`DataLoader, same tick|${big}`];
  const broken = results[`DataLoader, across ticks|${big}`];

  console.log(`  at limit ${big}:`);
  console.log(`    resolver per field        ${String(naive.queries).padStart(5)} queries`
    + `   p50 ${naive.p50.toFixed(1)}ms`);
  console.log(`    DataLoader, same tick     ${String(batched.queries).padStart(5)} queries`
    + `   p50 ${batched.p50.toFixed(1)}ms   `
    + `${(naive.p50 / Math.max(batched.p50, 1e-9)).toFixed(1)}x faster`);
  console.log(`    DataLoader, across ticks  ${String(broken.queries).padStart(5)} queries`
    + `   p50 ${broken.p50.toFixed(1)}ms`);
  console.log('');
  if (broken.queries >= naive.queries * 0.9) {
    console.log('  The third row has a DataLoader in it. It is constructed correctly, it is');
    console.log('  wired up correctly, and it batched nothing, because an `await` between');
    console.log('  two load() calls ends the tick and flushes a batch of one. Same query');
    console.log('  count as having no loader at all.');
    console.log('');
    console.log('  Nothing reports this. Not the loader, not the driver, not your logs. The');
    console.log('  only signal is queries-per-request -- which is why the counter in');
    console.log('  python/query_counter.py belongs in CI on the Node side too, and why');
    console.log('  "we use DataLoader" is not an answer to "how many queries does this');
    console.log('  endpoint issue".');
  }
  console.log('');
  console.log('  The batching window in one sentence: a loader can only batch the keys it');
  console.log('  is given before it flushes, and it flushes at the end of the tick. Ask for');
  console.log('  all your keys first, await afterwards.');

  await client.end();
}

main().catch((err) => { console.error(err); process.exit(1); });
