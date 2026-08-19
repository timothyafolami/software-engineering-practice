// Layer 4 Topic 7 (part 5) -- what makes THIS runtime stop renewing its lease.
//
// WHAT THIS DEMONSTRATES: a lease holder whose renewal timer targets a fixed
// interval, while Node's characteristic hazard is applied: one synchronous call
// on the event loop. Gaps between renewals are measured with
// process.hrtime.bigint() -- monotonic, so an NTP step cannot masquerade as a
// pause (Topic 3), which matters here because the question is a correctness one.
//
// Node's version of this is stricter than Python's and worth saying plainly:
// there is no threading module to escape to. `worker_threads` are separate
// isolates with their own heap, not shared-memory threads, so "just move it to a
// thread" costs you a structured-clone of everything the work touches. The
// escape hatch that IS free is libuv's thread pool -- crypto.pbkdf2, fs, dns --
// and it only covers what libuv already knows how to run off-thread. Arbitrary
// CPU-bound JS you wrote yourself is not on that list.
//
// WHAT TO LOOK FOR IN THE OUTPUT: the longest renewal gap against the 10s TTL,
// and that the async fix does the SAME amount of work in the same wall time. You
// did not make anything faster; you moved it off the resource the renewal needs.
//
//   node nodejs/pause_audit.js
//   node nodejs/pause_audit.js --block-seconds 3     // under the TTL
//   node nodejs/pause_audit.js --fixed               // only the fixed half

'use strict';

const crypto = require('node:crypto');
const os = require('node:os');

const LEASE_TTL_MS = 10_000;
const RENEW_INTERVAL_MS = 1_000;

function parseArgs(argv) {
  const args = { blockSeconds: 12, fixed: false };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--block-seconds') args.blockSeconds = Number(argv[++i]);
    else if (argv[i] === '--fixed') args.fixed = true;
    else { console.error(`unknown argument: ${argv[i]}`); process.exit(2); }
  }
  return args;
}

/** Renewal gaps, on a monotonic clock. Date.now() would work right up until an
 *  NTP correction invented a pause that never happened. */
class Renewals {
  constructor() { this.gaps = []; this.last = process.hrtime.bigint(); }
  tick() {
    const now = process.hrtime.bigint();
    this.gaps.push(Number(now - this.last) / 1e9);
    this.last = now;
  }
  longest() { return this.gaps.length ? Math.max(...this.gaps) : 0; }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** The hazard. A synchronous call: bcrypt.hashSync, a large JSON.parse, a
 *  hand-rolled hash loop. Nothing exotic and nothing that looks wrong in
 *  review -- and none of it yields. */
function blockingWork(seconds) {
  const end = process.hrtime.bigint() + BigInt(Math.round(seconds * 1e9));
  let digest = Buffer.from('seed');
  let rounds = 0;
  while (process.hrtime.bigint() < end) {
    digest = crypto.createHash('sha256').update(digest).digest();
    rounds++;
  }
  return rounds;
}

/** The fix. Same hashing, handed to libuv's thread pool via the async API, so
 *  the event loop stays free to fire the renewal timer. Note it is the SAME
 *  work: this is not an optimisation. */
async function offloadedWork(seconds) {
  const end = process.hrtime.bigint() + BigInt(Math.round(seconds * 1e9));
  let rounds = 0;
  while (process.hrtime.bigint() < end) {
    await new Promise((resolve, reject) => {
      crypto.pbkdf2('seed', 'salt', 20_000, 32, 'sha256', (err, key) => {
        if (err) reject(err); else resolve(key);
      });
    });
    rounds += 20_000;
  }
  return rounds;
}

async function run(blocking, blockSeconds) {
  const r = new Renewals();
  const timer = setInterval(() => r.tick(), RENEW_INTERVAL_MS);
  await sleep(2 * RENEW_INTERVAL_MS);

  const t0 = process.hrtime.bigint();
  const rounds = blocking ? blockingWork(blockSeconds) : await offloadedWork(blockSeconds);
  const workTook = Number(process.hrtime.bigint() - t0) / 1e9;

  await sleep(2 * RENEW_INTERVAL_MS);
  clearInterval(timer);
  return { longest: r.longest(), rounds, workTook };
}

function report(label, { longest, rounds, workTook }) {
  const lost = longest * 1000 > LEASE_TTL_MS;
  console.log(`  ${label.padEnd(26)}${longest.toFixed(2).padStart(9)}s    ` +
    `${workTook.toFixed(2).padStart(8)}s    ${(lost ? 'LOST THE LEASE' : 'held').padEnd(16)}` +
    `${String(rounds).padStart(12)} rounds`);
  return lost;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  console.log('='.repeat(78));
  console.log('Layer 4 Topic 7 -- Node.js pause audit');
  console.log('='.repeat(78));
  console.log(`  Node ${process.version} on ${process.platform} ${os.arch()}, ` +
    `${os.cpus().length} cores`);
  console.log(`  libuv thread pool: UV_THREADPOOL_SIZE=${process.env.UV_THREADPOOL_SIZE || '4 (default)'}`);
  console.log(`  lease TTL ${LEASE_TTL_MS / 1000}s, renewal every ${RENEW_INTERVAL_MS / 1000}s, ` +
    `hazard ${args.blockSeconds}s`);
  console.log('  hazard: ONE synchronous call on the event loop -- the whole hazard');
  console.log('  clock : process.hrtime.bigint(), so an NTP step is not a pause');
  console.log();
  console.log(`  ${'run'.padEnd(26)}${'longest gap'.padStart(10)}    ${'work took'.padStart(9)}` +
    `    ${'verdict'.padEnd(16)}`);

  let lost = false;
  if (!args.fixed) lost = report('blocking on the loop', await run(true, args.blockSeconds));
  report('libuv pool (async api)', await run(false, args.blockSeconds));

  console.log();
  console.log('  The timer was never cleared and never errored. setInterval does not');
  console.log('  fire while synchronous JS is running, because there is one thread and');
  console.log('  the synchronous call has it. The lease expired because the service');
  console.log('  was WORKING.');
  console.log();
  console.log('  The "rounds" column is not a benchmark and the two rows are not');
  console.log('  comparable -- one counts SHA-256 iterations, the other PBKDF2');
  console.log('  iterations. It is there to show that real work happened in both,');
  console.log('  and that the fix did not quietly do less of it.');
  console.log();
  console.log('  Fencing is what makes this survivable. A stale holder that resumes');
  console.log('  must be REJECTED BY THE RESOURCE -- `AND fence < $epoch` in the');
  console.log('  UPDATE -- because no amount of renewal tuning removes the pause.');
  process.exitCode = lost ? 1 : 0;
}

main().catch((err) => { console.error(err); process.exit(1); });
