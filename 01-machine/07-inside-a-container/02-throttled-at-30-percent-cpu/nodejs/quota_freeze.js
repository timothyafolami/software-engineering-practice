/*
 * 7.2 -- Node.js: one thread by design, and a hidden pool you did not size.
 *
 * WHAT THIS DEMONSTRATES
 *   The same experiment as the Python file next door, with the one thing
 *   only Node can show: your JavaScript runs on exactly ONE thread, so the
 *   "four runnable threads drain the bucket in 25ms" failure cannot happen
 *   to your handler code by accident. It has to be built on purpose, with
 *   worker_threads, which are separate V8 isolates rather than shared-memory
 *   threads.
 *
 *   And yet this process already has several OS threads before it runs a
 *   line of your code -- the census is printed, not guessed. libuv's thread
 *   pool (UV_THREADPOOL_SIZE,
 *   default 4, where fs, DNS and crypto live), V8's platform workers, and
 *   the GC helpers. Every one of them is inside your cgroup. Under
 *   `--cpus=1.0` a burst of concurrent `fs.readFile` or `crypto.pbkdf2`
 *   calls can drain your quota and freeze the event loop, and no amount of
 *   staring at your single-threaded handler code will explain it.
 *
 * WHAT TO LOOK FOR IN THE OUTPUT
 *   1. The at-rest thread census, printed before anything else. That is the
 *      number people are surprised by.
 *   2. Row 1 vs row 2: the same offered load and the same quota, only the
 *      worker count changes, and the throttle ratio moves while the average
 *      CPU does not.
 *   3. The heartbeat max gap. The heartbeat runs in its own worker and burns
 *      no measurable CPU. It is frozen anyway, because throttling dequeues
 *      every task in the cgroup, not just the greedy ones. That is your
 *      liveness probe failing on a container that looks idle.
 *
 * RUN
 *     node quota_freeze.js
 *
 *   On macOS the quota is a userspace model and the program says so. Inside
 *   a Linux container it reads /sys/fs/cgroup/cpu.max for ground truth.
 */
'use strict';

const os = require('os');
const fs = require('fs');
const crypto = require('crypto');
const { execSync } = require('child_process');
const {
  Worker, isMainThread, parentPort, workerData, threadId,
} = require('worker_threads');

// ---------------------------------------------------------------- config

const WORK_MS = 40;          // CPU cost of one request
const OFFERED_RATE = 9;      // req/s -> ~0.36 CPU of demand, well under quota
const RUN_SECONDS = 15;
const HEARTBEAT_MS = 10;
const PERIOD_US = 100000;

// Shared budget layout, as an Int32Array over a SharedArrayBuffer.
// Atomics.wait needs an Int32Array, which is why microseconds (not
// nanoseconds) are the unit: a period's worth of nanoseconds overflows
// int32 and the accounting would silently wrap.
const B_BALANCE = 0;       // microseconds left in this period
const B_GENERATION = 1;    // bumped on every refill; what workers wait on
const B_PERIODS = 2;
const B_THROTTLED = 3;     // periods in which someone was frozen
const B_FROZE_FLAG = 4;    // did anyone freeze in the current period
const B_USAGE_LO = 5;      // usage in microseconds, split to stay in int32
const B_USAGE_HI = 6;
const B_RUNNING = 7;
const B_SLOTS = 8;

const HASH_BLOCK = crypto.randomBytes(256 * 1024);

function nowMs() {
  // performance.timeOrigin is ms since the Unix epoch and differs per
  // worker, so adding it makes timestamps comparable ACROSS threads.
  return performance.timeOrigin + performance.now();
}

function burnCpu(targetMs, view) {
  // Wall time is charged rather than thread CPU time: Node has no
  // per-thread CPU clock, and this loop never blocks, so the two are the
  // same thing here. Python's version cannot make that assumption because
  // the GIL makes a thread's wall time include time spent not running.
  let spent = 0;
  while (spent < targetMs) {
    const mark = performance.now();
    const hash = crypto.createHash('sha256');
    for (let i = 0; i < 4; i++) hash.update(HASH_BLOCK);
    hash.digest();
    const slice = performance.now() - mark;
    spent += slice;
    if (view) chargeBudget(view, slice * 1000);
  }
}

function chargeBudget(view, micros) {
  Atomics.add(view, B_USAGE_LO, Math.round(micros));
  const balance = Atomics.sub(view, B_BALANCE, Math.round(micros)) - Math.round(micros);
  if (balance > 0) return;
  Atomics.store(view, B_FROZE_FLAG, 1);
  parkUntilRefill(view);
}

function parkUntilRefill(view) {
  while (Atomics.load(view, B_BALANCE) <= 0 && Atomics.load(view, B_RUNNING) === 1) {
    const generation = Atomics.load(view, B_GENERATION);
    // Atomics.wait blocks this OS thread outright, which is exactly what
    // the kernel does to a throttled task: dequeued, not spinning.
    Atomics.wait(view, B_GENERATION, generation, PERIOD_US / 1000);
  }
}

// ------------------------------------------------------------- worker side

if (!isMainThread) {
  const view = new Int32Array(workerData.budget);
  const dueTimes = new Float64Array(workerData.schedule);
  const cursor = new Int32Array(workerData.cursor);
  const role = workerData.role;

  if (role === 'heartbeat') {
    const gaps = [];
    let last = nowMs();
    let ticks = 0;
    const timer = setInterval(() => {
      // Burns no CPU, charges nothing -- and is frozen anyway.
      parkUntilRefill(view);
      const now = nowMs();
      gaps.push(now - last);
      last = now;
      ticks += 1;
      if (Atomics.load(view, B_RUNNING) === 0) {
        clearInterval(timer);
        parentPort.postMessage({ role, ticks, maxGap: Math.max(...gaps, 0) });
      }
    }, HEARTBEAT_MS);
  } else {
    const latencies = [];
    const loop = () => {
      while (Atomics.load(view, B_RUNNING) === 1) {
        const index = Atomics.load(cursor, 0);
        if (index >= dueTimes.length) break;
        const due = dueTimes[index];
        const now = nowMs();
        if (now < due) {
          // Nothing due yet. Sleep briefly instead of spinning; a spinning
          // worker would burn the very quota this experiment is measuring.
          Atomics.wait(view, B_GENERATION, Atomics.load(view, B_GENERATION),
            Math.min(due - now, 3));
          continue;
        }
        if (Atomics.compareExchange(cursor, 0, index, index + 1) !== index) continue;
        burnCpu(WORK_MS, view);
        latencies.push(nowMs() - due);
      }
      parentPort.postMessage({ role, latencies });
    };
    loop();
  }
  return;
}

// --------------------------------------------------------------- main side

function threadCensus() {
  try {
    if (fs.existsSync('/proc/self/status')) {
      const line = fs.readFileSync('/proc/self/status', 'utf8')
        .split('\n').find((l) => l.startsWith('Threads:'));
      return Number(line.split(/\s+/)[1]);
    }
    const out = execSync(`ps -M -p ${process.pid}`, { encoding: 'utf8' });
    return out.trim().split('\n').length - 1;
  } catch {
    return -1;
  }
}

function readCpuMax() {
  try {
    const raw = fs.readFileSync('/sys/fs/cgroup/cpu.max', 'utf8').trim();
    const [quota, period] = raw.split(' ');
    return quota === 'max' ? null : Number(quota) / Number(period || 100000);
  } catch {
    return null;
  }
}

function poissonSchedule(rate, seconds, seed) {
  // Deterministic LCG: the same arrival pattern every run, so two rows of
  // the table differ by the variable under test and nothing else.
  let state = seed >>> 0;
  const next = () => {
    state = (state * 1664525 + 1013904223) >>> 0;
    return (state + 1) / 4294967297;
  };
  const times = [];
  let t = 0;
  while (t < seconds) {
    times.push(t * 1000);
    t += -Math.log(next()) / rate;   // exponential inter-arrival
  }
  return times;
}

function percentile(sorted, p) {
  if (!sorted.length) return NaN;
  return sorted[Math.min(sorted.length - 1, Math.round((p / 100) * (sorted.length - 1)))];
}

function runVariant(workers, quotaCpus, periodUs) {
  return new Promise((resolve) => {
    const quotaUs = Math.round(quotaCpus * periodUs);
    const budget = new SharedArrayBuffer(B_SLOTS * 4);
    const view = new Int32Array(budget);
    Atomics.store(view, B_BALANCE, quotaUs);
    Atomics.store(view, B_RUNNING, 1);

    const relative = poissonSchedule(OFFERED_RATE, RUN_SECONDS, 20260818);
    const startMs = nowMs() + 300;   // let workers boot before the clock starts
    const scheduleBuf = new SharedArrayBuffer(relative.length * 8);
    new Float64Array(scheduleBuf).set(relative.map((ms) => startMs + ms));
    const cursorBuf = new SharedArrayBuffer(4);

    const pool = [];
    const latencies = [];
    let heartbeat = { ticks: 0, maxGap: NaN };
    let pending = workers + 1;

    const finish = () => {
      clearInterval(refill);
      const sorted = latencies.sort((a, b) => a - b);
      const usage = Atomics.load(view, B_USAGE_LO);
      resolve({
        completed: sorted.length,
        reqPerS: sorted.length / RUN_SECONDS,
        avgCpu: (100 * usage) / (RUN_SECONDS * 1e6),
        periods: Atomics.load(view, B_PERIODS),
        throttled: Atomics.load(view, B_THROTTLED),
        p50: percentile(sorted, 50),
        p99: percentile(sorted, 99),
        hbGap: heartbeat.maxGap,
        hbTicks: heartbeat.ticks,
      });
    };

    const spawn = (role) => {
      const worker = new Worker(__filename, {
        workerData: { budget, schedule: scheduleBuf, cursor: cursorBuf, role },
      });
      worker.on('message', (msg) => {
        if (msg.role === 'heartbeat') heartbeat = msg;
        else latencies.push(...msg.latencies);
        pending -= 1;
        worker.terminate();
        if (pending === 0) finish();
      });
      pool.push(worker);
      return worker;
    };

    for (let i = 0; i < workers; i++) spawn(`worker${i}`);
    spawn('heartbeat');

    // The refiller stands in for the kernel, so it lives on the MAIN thread
    // and is never itself frozen. The kernel is not inside your cgroup.
    const refill = setInterval(() => {
      if (Atomics.load(view, B_FROZE_FLAG) === 1) Atomics.add(view, B_THROTTLED, 1);
      Atomics.store(view, B_FROZE_FLAG, 0);
      Atomics.add(view, B_PERIODS, 1);
      Atomics.store(view, B_BALANCE, quotaUs);
      Atomics.add(view, B_GENERATION, 1);
      Atomics.notify(view, B_GENERATION);
    }, periodUs / 1000);

    setTimeout(() => {
      Atomics.store(view, B_RUNNING, 0);
      Atomics.add(view, B_GENERATION, 1);
      Atomics.notify(view, B_GENERATION);
    }, (RUN_SECONDS + 1.5) * 1000);
  });
}

function pad(value, width) {
  return String(value).padEnd(width);
}

async function main() {
  const quota = readCpuMax();
  console.log('7.2 -- throttled at 30% CPU: Node.js');
  console.log(`  runtime                : node ${process.version}, V8 ${process.versions.v8}`);
  console.log(`  os.cpus().length       : ${os.cpus().length}   <- host cores, always`);
  console.log(`  os.availableParallelism(): ${os.availableParallelism()}   <- cgroup-aware since libuv 1.49.1`);
  console.log(`  UV_THREADPOOL_SIZE     : ${process.env.UV_THREADPOOL_SIZE || '(unset -> 4)'}`);
  console.log(`  quota actually enforced: ${quota ? quota.toFixed(2) + ' CPU' : 'none (no cgroup on this host)'}`);
  console.log(`  OS threads at rest     : ${threadCensus()}  <- before any worker_threads exist`);
  console.log('');
  if (quota === null) {
    console.log('  !! FALLBACK: no /sys/fs/cgroup on this host');
    console.log('  !! This is a userspace MODEL of cpu.max, not the Linux kernel.');
    console.log('  !! Real numbers come from /sys/fs/cgroup/cpu.stat inside a container.');
    console.log('');
  }
  console.log(`  offered load: ${OFFERED_RATE} req/s x ${WORK_MS}ms CPU = ${(OFFERED_RATE * WORK_MS / 1000).toFixed(2)} CPU of demand`);
  console.log('  quota:        1.00 CPU. The demand is comfortably under the limit.');
  console.log(`  heartbeat wants a tick every ${HEARTBEAT_MS}ms; ${RUN_SECONDS}s per row`);
  console.log('');

  const variants = [
    ['4 worker_threads, 1.0 CPU (baseline)', 4, 1.0, PERIOD_US],
    ['fix 1: 1 worker_thread, 1.0 CPU', 1, 1.0, PERIOD_US],
    ['fix 2: 4 worker_threads, 2.0 CPU', 4, 2.0, PERIOD_US],
  ];

  const rows = [];
  for (const [label, workers, quotaCpus, periodUs] of variants) {
    const r = await runVariant(workers, quotaCpus, periodUs);
    rows.push([label, r.completed, r.reqPerS.toFixed(1), `${r.avgCpu.toFixed(0)}%`,
      `${r.throttled}/${r.periods}`,
      (r.periods ? r.throttled / r.periods : 0).toFixed(3),
      r.p50.toFixed(0), r.p99.toFixed(0), r.hbGap.toFixed(0)]);
    console.log(`  ran: ${label}`);
  }

  const headers = ['variant', 'n', 'req/s', 'avg CPU', 'throttled', 'ratio',
    'p50 ms', 'p99 ms', 'hb gap ms'];
  const widths = headers.map((h, i) =>
    Math.max(h.length, ...rows.map((r) => String(r[i]).length)));
  console.log('');
  console.log(headers.map((h, i) => pad(h, widths[i])).join('  '));
  console.log(widths.map((w) => '-'.repeat(w)).join('  '));
  for (const row of rows) console.log(row.map((c, i) => pad(c, widths[i])).join('  '));
  console.log('');
  console.log('  Node is the strictest version of the lesson in this lab: to get');
  console.log('  four runnable threads you had to WRITE four worker_threads. But');
  console.log(`  the at-rest census above says this process already had ${threadCensus()} OS`);
  console.log('  threads, and libuv will happily run four concurrent fs or crypto');
  console.log('  calls on them. Under a 1-CPU quota those four are enough to');
  console.log('  drain the bucket and freeze the event loop, and the stack trace');
  console.log('  you go looking at will be single-threaded and blameless.');
}

main();
