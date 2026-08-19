// Layer 7 · Topic 8 — Crypto hygiene and rate limiting (Node.js).
//
// One command, no deps: `node crypto_ratelimit.js`. Node's constant-time
// primitive is crypto.timingSafeEqual, and it THROWS if the two buffers differ
// in length -- which forces you to think about the length leak every
// constant-time compare has (comparing fixed-length HASHES removes it). argon2
// and bcrypt are native modules; the built-in memory-hard KDF is scrypt, used
// here for Part A. Three parts, measured at runtime.
//
// What to look for: scrypt is ~5-6 orders slower per verify than sha256; the
// naive compare's ns/op climbs with matching bytes while timingSafeEqual stays
// flat; in-proc(workers=4) enforces ~4x the configured limit; IP-keyed with 50
// IPs stops nothing.

const crypto = require('node:crypto');

function partA() {
  console.log('A. Hash cost (verifications/sec, measured)');
  const pw = Buffer.from('correct horse battery staple');

  let reps = 300000;
  let t0 = process.hrtime.bigint();
  for (let i = 0; i < reps; i++) crypto.createHash('sha256').update(pw).digest();
  const shaVps = reps / (Number(process.hrtime.bigint() - t0) / 1e9);
  console.log(`   sha256          ${shaVps.toFixed(0).padStart(14)} verify/sec`);

  // scrypt at a deliberately costly setting (N=2^15) -- the built-in slow KDF.
  const salt = crypto.randomBytes(16);
  reps = 50;
  t0 = process.hrtime.bigint();
  for (let i = 0; i < reps; i++) crypto.scryptSync(pw, salt, 32, { N: 32768, r: 8, p: 1, maxmem: 64 * 1024 * 1024 });
  const scVps = reps / (Number(process.hrtime.bigint() - t0) / 1e9);
  console.log(`   scrypt(N=2^15)  ${scVps.toFixed(1).padStart(14)} verify/sec`);

  const N = 10000, K = 1000000;
  console.log(`   crack-time model: attacker rig N=${N}x, list K=${K} candidates`);
  console.log(`      sha256: ${(K / (shaVps * N)).toFixed(6)} s to first crack`);
  console.log(`      scrypt: ${(K / (scVps * N)).toFixed(1)} s to first crack  -- ~${(shaVps / scVps).toFixed(0)}x slower per verify`);
  console.log('   (argon2id is the OWASP first choice; it needs a native module here.)\n');
}

let sink = 0;
function naiveEq(a, b) {
  if (a.length !== b.length) return 0;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return 0; // short-circuit
  return 1;
}

function partB() {
  console.log('B. Timing signal: naive short-circuit vs constant-time');
  const secret = crypto.randomBytes(32);
  const candidate = (matching) => {
    const c = Buffer.from(crypto.randomBytes(32));
    secret.copy(c, 0, 0, matching);
    if (matching < 32) c[matching] = secret[matching] ^ 0xff;
    return c;
  };
  const tse = (a, b) => (crypto.timingSafeEqual(a, b) ? 1 : 0);
  const avgNs = (fn, cand, reps) => {
    const t0 = process.hrtime.bigint();
    for (let i = 0; i < reps; i++) sink += fn(secret, cand);
    return Number(process.hrtime.bigint() - t0) / reps;
  };
  console.log('   matching leading bytes ->        avg ns/op');
  for (const [label, fn] of [['naive_eq', naiveEq], ['timingSafeEqual', tse]]) {
    let out = `   ${label.padEnd(20)}`;
    for (const k of [0, 8, 16, 31]) out += ` k=${k}:${avgNs(fn, candidate(k), 1000000).toFixed(2)}`;
    console.log(out);
  }
  console.log('   (naive trends up with k; timingSafeEqual flat. It THROWS on unequal');
  console.log('    lengths -- compare fixed-length hashes to avoid the length leak.)\n');
}

function partC() {
  console.log('C. Rate limiting: attempts-to-first-success and effective limit');
  const LIST = 1000, CORRECT_AT = 500, CONFIGURED = 10;
  const run = (mode, workers = 1, sourceIPs = 1) => {
    let allowed = 0, reached = false;
    const buckets = new Map();
    for (let i = 1; i <= LIST; i++) {
      const ip = i % sourceIPs;
      let key, permitted = false;
      if (mode === 'off') permitted = true;
      else {
        if (mode === 'redis_token_bucket') key = 'account';
        else if (mode === 'inproc') key = `w${i % workers}`;
        else key = `ip${ip}`;
        if (!buckets.has(key)) buckets.set(key, CONFIGURED);
        if (buckets.get(key) > 0) { buckets.set(key, buckets.get(key) - 1); permitted = true; }
      }
      if (permitted) { allowed++; if (i === CORRECT_AT) reached = true; }
    }
    return [allowed, reached];
  };
  const rows = [
    ['off', 1, 1, 'no limit'],
    ['redis_token_bucket', 1, 1, 'shared bucket, configured=10'],
    ['inproc', 1, 1, 'in-proc, 1 worker'],
    ['inproc', 4, 1, 'in-proc, 4 workers -> effective 4x'],
    ['ip_keyed', 1, 50, 'IP-keyed, attacker uses 50 IPs'],
  ];
  for (const [mode, w, ips, note] of rows) {
    const [allowed, reached] = run(mode, w, ips);
    console.log(`   ${mode.padEnd(18)} ${note.padEnd(34)} allowed=${String(allowed).padEnd(4)} ${reached ? 'reached password' : 'password NOT reached'}`);
  }
  console.log(`\n   effective/configured: inproc workers=4 allows ~${4 * CONFIGURED} vs configured ${CONFIGURED} -> 4x.`);
  console.log('   IP-keyed with 50 IPs lets the password through -> keying on IP is a fake fix.\n');
}

console.log('Layer 7 · Topic 8 — hash cost, timing signal, rate limiting\n');
partA(); partB(); partC();
if (sink === -1) console.log(sink); // keep sink live
console.log('Takeaway: password hash must be SLOW, a secret compare CONSTANT-TIME, and ' +
  'a rate limit keyed on the account with SHARED state.');
