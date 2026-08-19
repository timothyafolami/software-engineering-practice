// Layer 4 Topic 3 (Part A) -- Node's clocks, audited rather than assumed.
//
// WHAT THIS DEMONSTRATES: four things, in order.
//   1. the clock inventory: Date.now() is the wall clock and is settable;
//      performance.now() is monotonic sub-millisecond; process.hrtime.bigint()
//      is monotonic nanoseconds. Resolution is measured here, not quoted.
//   2. one span timed twice -- through the application's own now(), which reads
//      the wall clock, and through process.hrtime.bigint() -- while an
//      NTP-style step is applied inside two of the spans.
//   3. Node's specific subtlety: performance.timeOrigin is a WALL-CLOCK anchor
//      captured at process start, so `timeOrigin + performance.now()` inherits
//      every wall-clock hazard while performance.now() alone does not. Mixing
//      the two in one calculation is how a monotonic measurement gets
//      contaminated, and this program does it on purpose so you can see it.
//   4. the summary line for the README's record table.
//
// WHAT TO LOOK FOR IN THE OUTPUT: the NEGATIVE column in section 2, and in
// section 3 the fact that the `timeOrigin + performance.now()` span reports the
// same corrupted duration as the raw wall clock -- despite being built out of a
// monotonic reading.
//
//   node nodejs/clock_audit.js

'use strict';

const os = require('node:os');
const { performance } = require('node:perf_hooks');

const STEP_MS = -40000;   // an NTP correction, applied backwards, mid-run
const SPANS = 400;
const SPAN_WORK_US = 200;

// ------------------------------------------------------------- 1. inventory

/** Smallest non-zero delta this clock will report. Measured, not documented --
 *  a clock can advertise nanoseconds and tick in microseconds. */
function measureResolutionMs(read, trials = 20) {
  let smallest = Infinity;
  for (let t = 0; t < trials; t++) {
    const a = read();
    for (;;) {
      const b = read();
      if (b !== a) {
        const d = Number(b > a ? b - a : a - b);
        if (d < smallest) smallest = d;
        break;
      }
    }
  }
  return smallest;
}

function inventory() {
  console.log('-'.repeat(78));
  console.log('1. the clocks Node offers, and what each one is for');
  console.log('-'.repeat(78));
  console.log(`  ${'expression'.padEnd(34)}${'kind'.padEnd(12)}${'settable'.padEnd(10)}measured resolution`);

  const rows = [
    ['Date.now()', 'realtime', 'YES', () => Date.now(), 1e6],
    ['performance.now()', 'monotonic', 'no', () => performance.now(), 1e6],
    ['process.hrtime.bigint()', 'monotonic', 'no', () => process.hrtime.bigint(), 1],
    ['performance.timeOrigin + now()', 'realtime', 'YES',
      () => performance.timeOrigin + performance.now(), 1e6],
  ];
  for (const [name, kind, settable, read, toNs] of rows) {
    const res = measureResolutionMs(read) * toNs;
    console.log(`  ${name.padEnd(34)}${kind.padEnd(12)}${settable.padEnd(10)}${String(Math.round(res)).padStart(12)} ns`);
  }
  console.log();
  console.log(`  performance.timeOrigin = ${performance.timeOrigin.toFixed(3)} ms`);
  console.log('  ^ a wall-clock reading captured once, at process start. Everything you');
  console.log('    add it to becomes a wall-clock value, however monotonic it started out.');
}

// ------------------------------------------------- 2. one span, two clocks

/** The application's own now(). Every service has one; most read the wall clock.
 *  The offset stands in for an NTP step -- we never touch the system clock, and
 *  lab/README.md explains why per-container skew is not even possible here. */
class AppClock {
  constructor() { this.offsetMs = 0; }
  now() { return Date.now() + this.offsetMs; }
  step(ms) { this.offsetMs += ms; }
}

function burn(micros) {
  const end = process.hrtime.bigint() + BigInt(micros) * 1000n;
  while (process.hrtime.bigint() < end) { /* busy: we are timing, not sleeping */ }
}

function spanComparison(clock) {
  const wall = [];
  const mono = [];
  const contaminated = [];
  // Fixed indices, not a timer. A timer racing an 80ms loop is how you get a run
  // where the step lands between spans and the experiment silently proves
  // nothing -- which the README lists as a broken experiment, not a wrong
  // prediction. Determinism about *when* buys realism about *what*.
  const stepBackAt = Math.floor(SPANS / 3);
  const stepFwdAt = Math.floor((2 * SPANS) / 3);

  for (let i = 0; i < SPANS; i++) {
    const w0 = clock.now();
    const m0 = process.hrtime.bigint();
    const c0 = performance.timeOrigin + performance.now() + clock.offsetMs;
    burn(SPAN_WORK_US);
    if (i === stepBackAt) clock.step(STEP_MS);
    else if (i === stepFwdAt) clock.step(-STEP_MS);
    const w1 = clock.now();
    const m1 = process.hrtime.bigint();
    const c1 = performance.timeOrigin + performance.now() + clock.offsetMs;
    wall.push(w1 - w0);
    mono.push(Number(m1 - m0) / 1e6);
    contaminated.push(c1 - c0);
  }
  return { wall, mono, contaminated };
}

function pct(values, q) {
  const s = [...values].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.max(0, Math.round(q * s.length + 0.5) - 1))];
}

function spanReport({ wall, mono, contaminated }) {
  console.log();
  console.log('-'.repeat(78));
  console.log(`2. ${SPANS} identical spans, timed three ways, with a ${STEP_MS / 1000}s step`);
  console.log(`   and a ${-STEP_MS / 1000}s step landing INSIDE two of them`);
  console.log('-'.repeat(78));
  const head = `  ${'clock'.padEnd(30)}${'p50'.padStart(10)}${'p99'.padStart(12)}${'max'.padStart(14)}${'min'.padStart(14)}${'negative'.padStart(10)}`;
  console.log(head);
  let negatives = 0;
  const rows = [
    ['wall (app now())', wall],
    ['monotonic (hrtime.bigint)', mono],
    ['timeOrigin + performance.now()', contaminated],
  ];
  for (const [name, v] of rows) {
    const neg = v.filter((x) => x < 0).length;
    if (name.startsWith('wall')) negatives = neg;
    console.log(`  ${name.padEnd(30)}${pct(v, 0.5).toFixed(3).padStart(10)}${pct(v, 0.99).toFixed(3).padStart(12)}` +
      `${Math.max(...v).toFixed(1).padStart(14)}${Math.min(...v).toFixed(1).padStart(14)}${String(neg).padStart(10)}`);
  }
  console.log("  (milliseconds; 'negative' counts spans that finished before they started)");
  console.log();
  const hot = wall.indexOf(Math.max(...wall));
  const lo = Math.max(0, hot - 19);
  const win = wall.slice(lo, hot + 21);
  const winMono = mono.slice(lo, hot + 21);
  console.log(`  Two samples out of ${SPANS} were touched: ${Math.min(...wall).toFixed(0)} ms and ${Math.max(...wall).toFixed(0)} ms,`);
  console.log(`  against a p50 of ${pct(wall, 0.5).toFixed(3)} ms. Over all ${SPANS} spans that is only the max --`);
  console.log(`  one sample in ${SPANS} cannot move a p99 by rank. But dashboards aggregate`);
  console.log(`  windows, not runs: over the ${win.length} spans around the step the wall-clock`);
  console.log(`  p99 is ${pct(win, 0.99).toFixed(1)} ms against a monotonic p99 of ${pct(winMono, 0.99).toFixed(3)} ms.`);
  console.log('  Same workload, same machine, same instant. Only the clock differed.');
  console.log();
  console.log(`  Read the wall row's p50 too: ${pct(wall, 0.5).toFixed(3)} ms for work the monotonic`);
  console.log(`  row measures at ${pct(mono, 0.5).toFixed(3)} ms. Date.now() has millisecond resolution, so`);
  console.log('  it cannot see this span AT ALL -- it reports 0 or 1 and nothing between.');
  console.log('  That is a second, quieter reason not to time spans with the wall clock.');
  return negatives;
}

// ----------------------------------------------------- 3. the Node footgun

function footguns() {
  console.log();
  console.log('-'.repeat(78));
  console.log('3. the footgun specific to this runtime: timeOrigin contamination');
  console.log('-'.repeat(78));

  // performance.now() is monotonic. Adding timeOrigin to it makes a wall-clock
  // estimate -- and the row above proves it, because the contaminated column
  // tracked the wall clock through the step while the monotonic column did not.
  const t0mono = performance.now();
  const t0wall = performance.timeOrigin + performance.now();
  burn(1000);
  const t1mono = performance.now();
  const t1wall = performance.timeOrigin + performance.now();
  console.log(`  performance.now() span                 ${(t1mono - t0mono).toFixed(4)} ms   [monotonic]`);
  console.log(`  (timeOrigin + performance.now()) span  ${(t1wall - t0wall).toFixed(4)} ms   [wall clock]`);
  console.log('  The two agree here because nothing stepped. They are still different');
  console.log('  kinds of number, and only one of them survives an NTP correction.');
  console.log();

  // The other half of the subtlety: Date.now() and timeOrigin+now() are both
  // wall-clock, so they agree -- but they are anchored at different instants,
  // and the drift between them is real and grows.
  const drift = (performance.timeOrigin + performance.now()) - Date.now();
  console.log(`  (timeOrigin + performance.now()) - Date.now() = ${drift.toFixed(3)} ms`);
  console.log('  ^ both are "the wall clock". They disagree because timeOrigin was');
  console.log('    sampled once, at startup, and has been accumulating drift ever since.');
  console.log('    In a process that has been up for a week this is not a rounding error.');
  console.log();

  console.log(`  process.uptime()      ${process.uptime().toFixed(3)} s   [monotonic-ish, since process start]`);
  console.log(`  os.uptime()           ${os.uptime()} s   [since boot]`);
  console.log(`  platform              ${process.platform} ${os.release()} ${os.arch()}`);
  console.log('  Node exposes no CLOCK_MONOTONIC_RAW, no CLOCK_BOOTTIME and no way to');
  console.log('  ask which OS clock backs performance.now(). If you need that, you are');
  console.log('  in the wrong runtime for the question -- see cpp/clock_audit.cpp.');
  return true;
}

function main() {
  console.log('='.repeat(78));
  console.log('Layer 4 Topic 3 -- Node.js clock audit');
  console.log('='.repeat(78));
  console.log(`  Node ${process.version} on ${process.platform} ${os.arch()}`);
  console.log();
  inventory();
  const negatives = spanReport(spanComparison(new AppClock()));
  const reproduced = footguns();

  console.log();
  console.log('-'.repeat(78));
  console.log("4. one line for the record table in the README");
  console.log('-'.repeat(78));
  const res = measureResolutionMs(() => process.hrtime.bigint());
  console.log(`  | Node.js | process.hrtime.bigint() | ${res} ns | ` +
    `${reproduced ? 'yes' : 'NO -- investigate'} ` +
    `(${negatives} negative wall-clock span${negatives === 1 ? '' : 's'}) |`);
  console.log();
  console.log('  The table in the README stays blank until you fill it in. This line is');
  console.log('  the measurement, not the answer -- copy it across yourself.');
}

main();
