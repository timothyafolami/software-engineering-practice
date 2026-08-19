// Layer 2 · Topic 5 - Node: two DNS APIs that answer differently, and only one
// of them shares a thread pool with your file IO.
//
// `dns.lookup()` -- which every HTTP client in Node uses by default -- is
// getaddrinfo() dispatched to LIBUV'S THREAD POOL. That pool defaults to four
// threads and is shared with file IO, zlib and several crypto functions. Four
// concurrent slow lookups therefore stall unrelated file reads, and the
// symptom is a service that goes sluggish everywhere at once during a DNS
// blip, with nothing in the logs connecting the two.
//
// `dns.resolve()` is a different thing entirely: it speaks DNS over the
// network with c-ares, never touches the thread pool, and does NOT consult
// /etc/hosts or nsswitch -- which is why it sometimes returns a different
// answer than dns.lookup() for the same name. Phase C demonstrates that on
// your machine.
//
// Three phases:
//   A. The pool's size, and the baseline cost of a lookup and a file read.
//   B. Saturate the pool with crypto work and re-measure both. Nothing about
//      DNS changed; the lookup is queued behind pbkdf2.
//   C. lookup() vs resolve() for the same name, side by side.
//
// What to look for in the output:
//   - phase B: dns.lookup and fs.readFile BOTH inflated by the same event.
//     That shared fate is the whole lesson: raising UV_THREADPOOL_SIZE widens
//     the queue, it does not remove it.
//   - phase C: two APIs, one name, potentially two different answers.
//
// Run: node lookup_vs_resolve_threadpool.js
//      UV_THREADPOOL_SIZE=16 node lookup_vs_resolve_threadpool.js   (compare)
'use strict';

const dns = require('node:dns');
const fs = require('node:fs');
const crypto = require('node:crypto');

const POOL = Number(process.env.UV_THREADPOOL_SIZE || 4);
const CONCURRENCY = POOL * 3;

const ms = (t0) => Number(process.hrtime.bigint() - t0) / 1e6;

function timeIt(fn) {
  const t0 = process.hrtime.bigint();
  return fn().then(
    (v) => ({ ok: true, ms: ms(t0), v }),
    (e) => ({ ok: false, ms: ms(t0), v: e.code || e.message }),
  );
}

const lookup = (name) => new Promise((res, rej) =>
  dns.lookup(name, { all: true }, (e, a) => (e ? rej(e) : res(a))));

const resolve4 = (name) => new Promise((res, rej) =>
  dns.resolve4(name, (e, a) => (e ? rej(e) : res(a))));

const readFile = (path) => new Promise((res, rej) =>
  fs.readFile(path, (e, d) => (e ? rej(e) : res(d.length))));

// pbkdf2 is dispatched to the SAME libuv pool as dns.lookup and fs.readFile.
// This is not a contrived stand-in: bcrypt, zlib and every fs call land here.
const burn = () => new Promise((res, rej) =>
  crypto.pbkdf2('pw', 'salt', 400000, 64, 'sha512', (e) => (e ? rej(e) : res())));

async function stats(label, fn, n) {
  const rs = await Promise.all(Array.from({ length: n }, () => timeIt(fn)));
  const times = rs.map((r) => r.ms).sort((a, b) => a - b);
  const failed = rs.filter((r) => !r.ok).length;
  return {
    label,
    p50: times[Math.floor(times.length / 2)],
    max: times[times.length - 1],
    failed,
    sample: rs[0].v,
  };
}

function row(s, note) {
  console.log(`    ${s.label.padEnd(22)} p50 ${s.p50.toFixed(2).padStart(9)} ms   `
            + `max ${s.max.toFixed(2).padStart(9)} ms   ${s.failed ? `(${s.failed} failed) ` : ''}${note || ''}`);
}

async function main() {
  console.log('='.repeat(78));
  console.log('Node: the resolver, the file system and your crypto share four threads');
  console.log('='.repeat(78));
  console.log(`  node ${process.version}`);
  console.log(`  UV_THREADPOOL_SIZE = ${process.env.UV_THREADPOOL_SIZE || '(unset, so 4)'}`);
  console.log(`  measuring ${CONCURRENCY} concurrent operations of each kind`);
  console.log();

  console.log('  A. Baseline, nothing else running');
  // Both kinds measured at once, in both phases, because they compete for the
  // same four threads. Measuring them one after the other lets the pool drain
  // in between and hides the entire effect.
  const [idleLookup, idleRead] = await Promise.all([
    stats('dns.lookup', () => lookup('localhost'), CONCURRENCY),
    stats('fs.readFile', () => readFile('/etc/hosts'), CONCURRENCY),
  ]);
  row(idleLookup, 'getaddrinfo on the libuv pool');
  row(idleRead, 'also on the libuv pool');
  console.log();

  console.log('  B. The same two, with the pool saturated by pbkdf2');
  const burning = Array.from({ length: CONCURRENCY }, () => burn());
  await new Promise((r) => setTimeout(r, 50));       // let the pool fill
  const [busyLookup, busyRead] = await Promise.all([
    stats('dns.lookup', () => lookup('localhost'), CONCURRENCY),
    stats('fs.readFile', () => readFile('/etc/hosts'), CONCURRENCY),
  ]);
  await Promise.all(burning);
  row(busyLookup, `queued behind ${CONCURRENCY} pbkdf2 calls`);
  row(busyRead, 'stalled by the SAME event');
  console.log();
  console.log(`    lookup inflation  ${(busyLookup.p50 / Math.max(idleLookup.p50, 1e-6)).toFixed(0)}x at p50`);
  console.log(`    readFile inflation ${(busyRead.p50 / Math.max(idleRead.p50, 1e-6)).toFixed(0)}x at p50`);
  console.log('    Nothing about DNS changed. No packet was sent. A password hash made');
  console.log('    your name resolution slow, and it made your file reads slow at the');
  console.log('    same instant -- which is why this shows up as "the service went');
  console.log('    sluggish everywhere at once" rather than as a DNS incident.');
  console.log();
  console.log('    Raising UV_THREADPOOL_SIZE widens that queue; it does not remove it.');
  console.log('    Re-run with UV_THREADPOOL_SIZE=16 and watch the inflation shrink but');
  console.log('    not vanish -- Topic 2\'s lesson, in a pool nobody thinks of as a pool.');
  console.log();

  console.log('  C. dns.lookup() vs dns.resolve4(): two APIs, one name');
  for (const name of ['localhost', 'example.com']) {
    const l = await timeIt(() => lookup(name));
    const r = await timeIt(() => resolve4(name));
    const fmt = (x) => (x.ok
      ? `${JSON.stringify(Array.isArray(x.v) ? x.v.slice(0, 2) : x.v)} in ${x.ms.toFixed(1)} ms`
      : `${x.v} after ${x.ms.toFixed(1)} ms`);
    console.log(`    ${name}`);
    console.log(`      dns.lookup    ${fmt(l)}`);
    console.log(`      dns.resolve4  ${fmt(r)}`);
  }
  console.log();
  console.log('    Look at the times as well as the answers. lookup() answered `localhost`');
  console.log('    nsswitch, and returns whatever your OS says. resolve4() sends a DNS');
  console.log('    query over the network with c-ares and knows nothing about either.');
  console.log('    For a name defined only in /etc/hosts -- localhost, a docker-compose');
  console.log('    service alias, a hosts-file override you added during an incident --');
  console.log('    they can and do disagree, and no amount of reading the DNS server\'s');
  console.log('    logs will explain the difference.');
  console.log();
  console.log('  And the thing this topic keeps coming back to: neither of these caches');
  console.log('  anything. Node honours no TTL of its own. Whatever kept your service');
  console.log('  talking to a dead address was your connection pool, not either of the');
  console.log('  two functions above.');
}

main();
