// Layer 7 · Topic 2 — SQL injection as a string-building failure (Node.js).
//
// One command, no arguments, no external deps: `node sqli.js`.
// Uses the built-in `node:sqlite` (Node 22+/24) so it runs with zero install;
// the injection mechanism is identical to `pg` against Postgres, only the
// placeholder token differs (`?` here, `$1` in pg).
//
// The README's Node-specific trap is the template-literal libraries: in
// `postgres.js`, sql`... WHERE email = ${email}` is a TAGGED TEMPLATE that
// parameterizes, while the same-looking string passed to pool.query() is raw
// interpolation and is injectable. Two nearly identical character sequences,
// opposite safety. Below, `searchVulnerable` is the raw-interpolation shape
// and `searchParameterized` binds with `?` -- know which one your file uses.
//
// What to look for: tautology dumps all 3 users when vulnerable, 0 rows (no
// error) when parameterized; UNION steals the key only when vulnerable; the
// blind channel recovers all 32 chars in ~linear requests.

const { DatabaseSync } = require('node:sqlite');

const SECRET_KEY = 'S3CR3T_KEY_abcdef0123456789abcd0'; // 32 chars
const CHARSET =
  'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_';

function seed() {
  const db = new DatabaseSync(':memory:');
  db.exec(`CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, name TEXT);
           CREATE TABLE api_keys (user_id INTEGER PRIMARY KEY, key TEXT);`);
  ['alice', 'bob', 'carol'].forEach((n, i) =>
    db.prepare('INSERT INTO users VALUES (?,?,?)').run(i + 1, `${n}@lab.test`, n));
  db.prepare('INSERT INTO api_keys VALUES (1, ?)').run(SECRET_KEY);
  return db;
}

function searchVulnerable(db, email) {
  // THE BUG: attacker bytes become SQL syntax.
  const sql = `SELECT id, email, name FROM users WHERE email = '${email}'`;
  try { return { rows: db.prepare(sql).all() }; }
  catch (e) { return { err: e.message }; }
}
function searchParameterized(db, email) {
  try { return { rows: db.prepare('SELECT id, email, name FROM users WHERE email = ?').all(email) }; }
  catch (e) { return { err: e.message }; }
}
function listVulnerable(db, sort) {
  try { return { rows: db.prepare(`SELECT id, email, name FROM users ORDER BY ${sort}`).all() }; }
  catch (e) { return { err: e.message }; }
}
function listAllowlist(db, sort) {
  const allowed = new Set(['id', 'email', 'name']);
  if (!allowed.has(sort)) return { err: `rejected identifier '${sort}' (not in allowlist)` };
  return { rows: db.prepare(`SELECT id, email, name FROM users ORDER BY ${sort}`).all() };
}

function partAB(db) {
  console.log(`Payload 1 — boolean tautology  "' OR '1'='1"`);
  for (const [label, fn] of [['vulnerable', searchVulnerable], ['parameterized', searchParameterized]]) {
    const r = fn(db, "' OR '1'='1");
    console.log(`   ${label.padEnd(14)} -> ` +
      (r.rows ? `${r.rows.length} rows: ${JSON.stringify(r.rows.map(x => x.name))}` : `ERROR: ${r.err}`));
  }

  console.log(`\nPayload 2 — UNION cross-table (steal api_keys.key)`);
  const union = "' UNION SELECT user_id, key, key FROM api_keys--";
  for (const [label, fn] of [['vulnerable', searchVulnerable], ['parameterized', searchParameterized]]) {
    const r = fn(db, union);
    if (r.err) { console.log(`   ${label.padEnd(14)} -> ERROR: ${r.err}`); continue; }
    const leaked = r.rows.find(x => x.email === SECRET_KEY);
    console.log(`   ${label.padEnd(14)} -> ${r.rows.length} rows; secret leaked: ${leaked ? leaked.email : 'no'}`);
  }

  console.log(`\nPayload 4 — identifier injection on ORDER BY (cannot be bound)`);
  for (const [label, fn] of [['parameterized*', listVulnerable], ['allowlist', listAllowlist]]) {
    const r = fn(db, '(SELECT key FROM api_keys LIMIT 1)');
    console.log(`   ${label.padEnd(14)} -> ` +
      (r.rows ? `${r.rows.length} rows (injection ran in ORDER BY!)` : r.err));
  }
  console.log('   *parameterizing WHERE does not close ORDER BY: the column ' +
    'position is fixed at parse time, before any bound value exists.');
}

function partCBlind(db) {
  console.log('\nPayload 3 — boolean-blind extraction of the 32-char key');
  let recovered = '', requests = 0;
  const t0 = process.hrtime.bigint();
  for (let pos = 1; pos <= SECRET_KEY.length; pos++) {
    for (const ch of CHARSET) {
      requests++;
      const payload = `nope' OR substr((SELECT key FROM api_keys WHERE user_id=1),${pos},1)='${ch}`;
      if (searchVulnerable(db, payload).rows.length > 0) { recovered += ch; break; }
    }
  }
  const ms = Number(process.hrtime.bigint() - t0) / 1e6;
  const n = SECRET_KEY.length;
  const linear = (n * (CHARSET.length + 1)) >> 1, binsearch = n * 7;
  console.log(`   recovered: ${recovered}`);
  console.log(`   correct:   ${recovered === SECRET_KEY ? 'YES' : 'NO'}`);
  console.log(`   requests to recover ${n} chars, one char/request (measured): ${requests}`);
  console.log(`   wall-clock: ${ms.toFixed(0)} ms`);
  console.log(`   theory: linear ~${linear} req; binary-search per char ~${binsearch} req ` +
    `(ratio ~${(linear / binsearch).toFixed(1)}x) -- "blind" costs the attacker requests, not success.`);
}

console.log('Layer 7 · Topic 2 — SQL injection (Node / node:sqlite)\n');
const db = seed();
partAB(db);
partCBlind(db);
console.log('\nTakeaway: the safe path binds the value; the value never enters ' +
  'the parsed SQL. Same principle as XSS (Topic 3) and command injection: ' +
  'keep attacker bytes out of any interpreter reached as code.');
