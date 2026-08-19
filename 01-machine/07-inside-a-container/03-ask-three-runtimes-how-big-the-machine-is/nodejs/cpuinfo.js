// 7.3 -- Node: mostly fixed, in two places, at two different times.
//
// WHAT THIS DEMONSTRATES
//   Node has two CPU-count calls that look interchangeable at review time
//   and are not:
//
//     os.cpus().length            -> HOST logical CPUs. Still wrong.
//     os.availableParallelism()   -> delegates to libuv's
//                                    uv_available_parallelism(), which since
//                                    libuv 1.49 factors in the cgroup CPU
//                                    quota on Linux. Right.
//
//   The modern call is correct and the old one is not, which is a nastier
//   trap than a call that is simply broken: `os.cpus().length` reads like
//   perfectly reasonable code, and nothing flags it. This probe prints
//   process.versions.uv rather than trusting any version number in a README
//   (including this one) -- the libuv version is the thing that decides.
//
//   Two more numbers matter and neither is a CPU count:
//     * UV_THREADPOOL_SIZE (default 4) sizes the pool that runs fs, DNS and
//       crypto. It is read ONCE at process start, so setting
//       process.env.UV_THREADPOOL_SIZE from inside your own code is usually
//       too late -- this probe demonstrates that by trying it.
//     * V8's old-space heap limit, which Node DOES size from the cgroup
//       memory limit via uv_get_constrained_memory. Memory is Node's better
//       half, and 7.6 is where that pays off.
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. The two CPU calls next to the enforced quota. Inside a container
//      under `--cpus=1.5` they disagree with each other, and only one of
//      them agrees with the kernel.
//   2. The thread census. A Node process at rest routinely has ~10 OS
//      threads -- libuv's pool plus V8's background compilation and
//      concurrent-marking GC threads -- so the runtime with the strongest
//      "I only use one core" reputation drains a 1-CPU bucket faster than
//      its reputation implies.
//   3. The UV_THREADPOOL_SIZE experiment at the bottom: the env var is set,
//      and the pool it was supposed to size has already been built.
//
// RUN
//   node cpuinfo.js
//
//   Inside a Linux container, which is where the columns separate:
//     docker run --rm --cpus=1.5     -v "$PWD:/w" -w /w node:24-slim node cpuinfo.js
//     docker run --rm --cpuset-cpus=0,1 -v "$PWD:/w" -w /w node:24-slim node cpuinfo.js
//
//   On macOS there is no /sys/fs/cgroup, so the quota row reads n/a. That is
//   the correct answer on Darwin, not a failure.

'use strict';

const os = require('os');
const fs = require('fs');
const v8 = require('v8');

// --------------------------------------------------------------- the kernel

// CPUs of bandwidth the cgroup actually enforces, or null for no ceiling.
// Twenty lines, no dependencies. This is what uv_available_parallelism()
// does for you -- and what os.cpus() does not.
function readCpuMax() {
  try {
    const raw = fs.readFileSync('/sys/fs/cgroup/cpu.max', 'utf8').trim();
    const [quota, period = '100000'] = raw.split(/\s+/);
    if (quota === 'max') return null;
    return Number(quota) / Number(period);
  } catch {
    try {
      const quota = Number(fs.readFileSync('/sys/fs/cgroup/cpu/cpu.cfs_quota_us', 'utf8'));
      const period = Number(fs.readFileSync('/sys/fs/cgroup/cpu/cpu.cfs_period_us', 'utf8'));
      return quota > 0 ? quota / period : null;
    } catch {
      return null;  // no cgroupfs at all: every macOS host
    }
  }
}

function readFileOrNull(path) {
  try {
    return fs.readFileSync(path, 'utf8').trim();
  } catch {
    return null;
  }
}

function memoryMax() {
  const raw = readFileOrNull('/sys/fs/cgroup/memory.max');
  if (raw && raw !== 'max') return Number(raw);
  const v1 = readFileOrNull('/sys/fs/cgroup/memory/memory.limit_in_bytes');
  // v1 spells "unlimited" as a number near 2^63, not as a word.
  if (v1 && Number(v1) < 2 ** 62) return Number(v1);
  return null;
}

// OS threads this process has right now. /proc does not exist on Darwin, so
// there are two genuinely different mechanisms rather than one that quietly
// returns a wrong number on the second platform.
function threadCensus() {
  const status = readFileOrNull('/proc/self/status');
  if (status) {
    const line = status.split('\n').find((l) => l.startsWith('Threads:'));
    if (line) return Number(line.split(/\s+/)[1]);
  }
  if (process.platform === 'darwin') {
    try {
      const { execSync } = require('child_process');
      const out = execSync(`ps -M -p ${process.pid}`, { encoding: 'utf8' });
      return Math.max(1, out.trim().split('\n').length - 1);
    } catch {
      return null;
    }
  }
  return null;
}

// ------------------------------------------------------------------- output

function table(headers, rows) {
  const widths = headers.map((h, i) =>
    Math.max(h.length, ...rows.map((r) => String(r[i]).length)));
  const line = (cells) =>
    cells.map((c, i) => String(c).padEnd(widths[i])).join('  ');
  console.log(line(headers));
  console.log(line(widths.map((w) => '-'.repeat(w))));
  for (const row of rows) console.log(line(row));
}

function main() {
  const quota = readCpuMax();
  const hostCpus = os.cpus().length;
  const parallelism = typeof os.availableParallelism === 'function'
    ? os.availableParallelism()
    : null;

  console.log('7.3 -- how big is this machine? Node\'s answers');
  console.log(`  runtime     : node ${process.version} on ${process.platform}/${process.arch}`);
  console.log(`  V8          : ${process.versions.v8}`);
  console.log(`  libuv       : ${process.versions.uv}   <- THIS is what decides whether`);
  console.log('                availableParallelism() reads the cgroup quota.');
  console.log('                Quota awareness landed in libuv 1.49; do not take a');
  console.log('                version number from a README, including this one.');
  console.log();

  table(
    ['what people call', 'the call', 'answer here', 'which question it answers', 'what it tracks'],
    [
      ['os.cpus().length', 'os.cpus().length', hostCpus,
        '(1) how big is the machine', 'nothing -- the host, always'],
      ['os.availableParallelism()', 'os.availableParallelism()',
        parallelism === null ? 'n/a (node < 18.14)' : parallelism,
        '(2)+(3) affinity AND quota', 'uv_available_parallelism()'],
      ['/sys/fs/cgroup/cpu.max', 'fs.readFileSync(...)',
        quota === null ? 'n/a' : quota.toFixed(2),
        '(3) how much CPU TIME may I consume', 'cpu.max -- THE ENFORCED NUMBER'],
    ]);
  console.log();

  console.log('  ground truth on this host:');
  console.log(`    cpu.max               ${quota === null ? 'no ceiling / no cgroupfs' : quota.toFixed(2) + ' CPU'}`);
  console.log(`    cpuset.cpus.effective ${readFileOrNull('/sys/fs/cgroup/cpuset.cpus.effective') ?? 'n/a'}`);
  const memLimit = memoryMax();
  console.log(`    memory.max            ${memLimit === null ? 'no ceiling / n/a' : (memLimit / 2 ** 20).toFixed(0) + ' MiB'}`);
  console.log(`    os.totalmem()         ${(os.totalmem() / 2 ** 20).toFixed(0)} MiB   <- the HOST's memory (7.6)`);
  console.log();

  if (quota === null) {
    console.log('  NOTE: no CPU quota is enforced here, so the two runtime rows agree');
    console.log('        and the matrix has one column. That is the correct result on');
    console.log('        this host. Run it under --cpus=1.5 and the rows separate.');
    console.log();
  }

  // ---- the parts of Node that are not a CPU count ----------------------
  console.log('  The numbers Node sizes itself from, which are NOT the CPU count:');
  const heapLimitMb = v8.getHeapStatistics().heap_size_limit / 2 ** 20;
  console.log(`    V8 old-space heap limit  ${heapLimitMb.toFixed(0)} MiB`);
  console.log('      Node passes the cgroup memory limit to V8 via');
  console.log('      uv_get_constrained_memory, so this IS container-aware -- the one');
  console.log('      place Node is more correct than every other runtime here. Compare');
  console.log(`      it against memory.max above; --max-old-space-size overrides it.`);
  console.log(`    UV_THREADPOOL_SIZE       ${process.env.UV_THREADPOOL_SIZE ?? '<unset> (default 4)'}`);
  console.log('      Sizes the pool running fs, DNS and crypto. It is a separate number');
  console.log('      from every CPU count above, and those threads spend the same quota.');
  console.log(`    OS threads right now     ${threadCensus() ?? 'n/a'}`);
  console.log('      libuv pool + V8 background compilation + concurrent-marking GC.');
  console.log('      "Single-threaded" is a statement about your JavaScript, not about');
  console.log('      the process, and the cgroup charges the process.');
  console.log();

  // ---- the trap that costs people an afternoon --------------------------
  console.log('  Demonstration: UV_THREADPOOL_SIZE is read ONCE, at process start.');
  const before = process.env.UV_THREADPOOL_SIZE ?? '<unset>';
  process.env.UV_THREADPOOL_SIZE = '64';
  console.log(`    before  process.env.UV_THREADPOOL_SIZE = ${before}`);
  console.log(`    now     process.env.UV_THREADPOOL_SIZE = ${process.env.UV_THREADPOOL_SIZE}`);
  console.log('    ...and the pool that value was supposed to size was built before');
  console.log('    this file was parsed. The assignment above changed a string and');
  console.log('    nothing else. It has to be an environment variable set by whatever');
  console.log('    starts the process -- the same place that sets `cpus:`.');
  console.log();

  const honest = quota === null ? null : Math.max(1, Math.floor(quota));
  if (honest !== null) {
    console.log(`  What the quota says a CPU-bound service should run: ${honest} process(es)`);
    console.log(`    os.cpus().length would have told you ${hostCpus}.`);
  }
  console.log('  Do not size a pool from os.cpus().length. It has never once been the');
  console.log('  number the kernel enforces, and it never says so.');
}

main();
