// Layer 3, Topic 4 - node-postgres and the OTHER protocol default.
//
// WHAT IT DEMONSTRATES: `pg` is the mirror image of pgx. It sends UNNAMED
// extended-protocol statements by default, so the server parses and plans every
// execution with the real parameter values in front of it. Three consequences,
// all measured below:
//
//   1. You always get a CUSTOM plan. The generic-plan trap that Go's
//      golang/plan_cache demonstrates cannot happen here, because there is no
//      cached plan to promote to generic. Skewed data is safe by default.
//   2. Nothing is left on the server. `pg_prepared_statements` stays empty
//      after any number of executions -- which is exactly why `pg` works behind
//      a transaction-mode PgBouncer with zero configuration, while drivers that
//      prepare need care. That is Topic 7's problem, and this is the property
//      that decides it.
//   3. You pay planning time on every single execution. That is the bill for
//      the two properties above, and this program prints it.
//
// It also opts INTO named statements -- `pg` supports them, one `name:` field --
// and shows the same server-side object appearing, and the same generic-plan
// switch at the sixth execution that the Go program shows. The default is a
// choice, not a limitation.
//
// WHAT TO LOOK FOR: the per-execution timings for unnamed vs named on a cheap
// query, and the `Planning Time` line under each. On a point lookup, planning
// is most of the work.
//
// Run:  node 04-reading-a-query-plan/nodejs/protocol_plans.js
// DSN:  LAB_PG_URL, default postgres:///sep_lab_03_data?host=/tmp
// Dep:  npm install --prefix 04-reading-a-query-plan/nodejs

'use strict';

let Client;
try {
  ({ Client } = require('pg'));
} catch (err) {
  console.error('This program needs node-postgres.');
  console.error('  unblock: npm install --prefix 04-reading-a-query-plan/nodejs');
  console.error('  (package.json beside this file declares it; nothing else is needed)');
  process.exit(1);
}

const DSN = process.env.LAB_PG_URL || 'postgres:///sep_lab_03_data?host=/tmp';
const RARE = 'failed';      // ~1% of orders
const COMMON = 'complete';  // ~92% of orders
const REPEATS = 500;

const SQL = 'SELECT count(*), sum(total_cents) FROM orders WHERE status = $1';
const POINT_SQL = 'SELECT customer_id, status FROM orders WHERE id = $1';

function planOf(rows) {
  const text = rows.map((r) => r['QUERY PLAN'].trim()).join(' | ');
  let scan = 'seq scan';
  if (text.includes('Index Scan')) scan = 'index scan';
  else if (text.includes('Bitmap')) scan = 'bitmap';
  const generic = text.includes('$1');
  const cond = text.split(' | ').find((p) => p.includes('Cond:') || p.includes('Filter:')) || '-';
  return { scan, generic, cond };
}

async function timed(fn, n) {
  const t0 = process.hrtime.bigint();
  for (let i = 0; i < n; i += 1) await fn(i);
  return Number(process.hrtime.bigint() - t0) / 1e6;
}

async function planningTime(client, sql, value) {
  // ANALYZE so the number is measured rather than estimated. Planning Time is
  // reported separately from Execution Time, which is the whole point here.
  const { rows } = await client.query(`EXPLAIN (ANALYZE, FORMAT JSON) ${sql}`, [value]);
  const plan = rows[0]['QUERY PLAN'][0];
  return { planning: plan['Planning Time'], execution: plan['Execution Time'] };
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

  const { rows: [v] } = await client.query('SELECT version()');
  console.log('='.repeat(78));
  console.log('node-postgres: unnamed statements, custom plans, and what they cost');
  console.log('='.repeat(78));
  console.log(v.version.split(' on ')[0]);

  await client.query('SET random_page_cost = 1.1');
  await client.query('SET effective_cache_size = \'1GB\'');
  await client.query('CREATE INDEX IF NOT EXISTS idx_protocol_status ON orders (status)');
  await client.query('ANALYZE orders');

  try {
    // ------------------------------------------------------------------
    // 1. The default: unnamed. Ten executions, alternating values.
    // ------------------------------------------------------------------
    await client.query('DEALLOCATE ALL');
    console.log(`\n  ten UNNAMED executions of the same query, alternating values:`);
    console.log(`    ${'exec'.padEnd(6)}${'value'.padEnd(11)}${'scan'.padEnd(12)}${'plan'.padEnd(9)}condition`);
    for (let i = 1; i <= 10; i += 1) {
      const value = i % 2 === 0 ? COMMON : RARE;
      await client.query(SQL, [value]);
      const { rows } = await client.query(`EXPLAIN ${SQL}`, [value]);
      const p = planOf(rows);
      console.log(`    ${String(i).padEnd(6)}${value.padEnd(11)}${p.scan.padEnd(12)}`
        + `${(p.generic ? 'GENERIC' : 'custom').padEnd(9)}${p.cond}`);
    }

    const { rows: left } = await client.query(
      'SELECT count(*)::int AS n FROM pg_prepared_statements');
    console.log(`    server-side prepared statements left behind: ${left[0].n}`);
    console.log('    Every execution got a plan built for the value actually supplied, and');
    console.log('    the server is holding nothing on this connection\'s behalf.');

    // ------------------------------------------------------------------
    // 2. Opting in to named statements: the same object pgx creates by default.
    // ------------------------------------------------------------------
    console.log(`\n  the same query as a NAMED statement -- one extra field, opted into:`);
    console.log(`    ${'exec'.padEnd(6)}${'value'.padEnd(11)}${'scan'.padEnd(12)}${'plan'.padEnd(9)}condition`);
    // A SQL-level twin of the named statement, so EXPLAIN can see the cached
    // plan: `EXPLAIN` cannot reach into the driver's own named statement, but
    // both land in the same per-session plan cache and follow the same rule.
    await client.query(`PREPARE ps_sql(text) AS ${SQL}`);
    for (let i = 1; i <= 8; i += 1) {
      await client.query({ name: 'ps_common', text: SQL, values: [COMMON] });
      // COMMON is a program constant, not input; interpolating input here
      // would be a SQL injection.
      const { rows } = await client.query(`EXPLAIN EXECUTE ps_sql('${COMMON}')`);
      const p = planOf(rows);
      console.log(`    ${String(i).padEnd(6)}${COMMON.padEnd(11)}${p.scan.padEnd(12)}`
        + `${(p.generic ? 'GENERIC' : 'custom').padEnd(9)}${p.cond}`);
    }
    const { rows: named } = await client.query(
      'SELECT name, generic_plans, custom_plans FROM pg_prepared_statements ORDER BY name');
    console.log('    server-side prepared statements now:');
    for (const r of named) {
      console.log(`      ${r.name.padEnd(14)} generic=${r.generic_plans} custom=${r.custom_plans}`);
    }
    console.log('    Same server, same switch at execution six. The driver default is what');
    console.log('    differs between this program and golang/plan_cache -- nothing else.');

    // ------------------------------------------------------------------
    // 3. What the default costs: planning, every time.
    // ------------------------------------------------------------------
    console.log(`\n  ${REPEATS} executions of a cheap point lookup, unnamed vs named:`);
    await client.query('DEALLOCATE ALL');

    const unnamedMs = await timed(
      (i) => client.query(POINT_SQL, [(i % 1000) + 1]), REPEATS);
    const namedMs = await timed(
      (i) => client.query({ name: 'ps_point', text: POINT_SQL, values: [(i % 1000) + 1] }), REPEATS);

    const pt = await planningTime(client, POINT_SQL, 424242);
    console.log(`    ${'mode'.padEnd(10)}${'total ms'.padStart(10)}${'per exec ms'.padStart(14)}`);
    console.log(`    ${'unnamed'.padEnd(10)}${unnamedMs.toFixed(1).padStart(10)}`
      + `${(unnamedMs / REPEATS).toFixed(3).padStart(14)}`);
    console.log(`    ${'named'.padEnd(10)}${namedMs.toFixed(1).padStart(10)}`
      + `${(namedMs / REPEATS).toFixed(3).padStart(14)}`);
    console.log(`    server-side, that same query costs ${pt.planning.toFixed(3)} ms to PLAN `
      + `and ${pt.execution.toFixed(3)} ms to RUN.`);
    console.log('    The gap between the two rows should be close to that planning figure:');
    console.log('    it is what you pay 500 times instead of once. Both rows also include a');
    console.log('    round trip, which on a unix socket is small and over a network is not.');

    console.log('\n  The trade, stated once:');
    console.log('    unnamed  -> correct plan for every value, nothing held on the server,');
    console.log('                works behind a transaction-mode pooler untouched, pays');
    console.log('                planning on every execution.');
    console.log('    named    -> planning paid once, a server object tied to this connection,');
    console.log('                and the generic-plan switch waiting at execution six.');
    console.log('    Neither is the right answer. Knowing which one your driver picked for you');
    console.log('    is the point -- and most people using either have never checked.');
  } finally {
    await client.query('DROP INDEX IF EXISTS idx_protocol_status');
    await client.end();
    console.log('\n(index dropped -- this program leaves the lab as it found it)');
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
