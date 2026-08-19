// 7.6 -- Node: the only runtime here that is container-aware by default on
// memory, and the one whose failure mode is easiest to mistake for the other.
//
// WHAT THIS DEMONSTRATES
//   libuv's uv_get_constrained_memory() reads the cgroup limit and V8 sizes
//   its default old-space heap from it, so a Node process in a 256MB
//   container does not plan for the host's 32GB. That is a genuine
//   advantage over every other runtime in this folder.
//
//   The consequence is that Node has TWO different deaths, with two
//   different exit codes, and telling them apart in a restart log is the
//   practical skill this file builds:
//
//     * JS HEAP EXHAUSTION. V8 hits its old-space ceiling and aborts with
//       "FATAL ERROR: ... Reached heap limit Allocation failed" AND A STACK
//       TRACE. Exit code 134 (128 + SIGABRT). You get told what happened.
//     * CGROUP OOM KILL. The container exceeds memory.max and the kernel
//       sends SIGKILL. Nothing is printed. Exit code 137.
//
//   And the trap that makes it worth running both modes: Buffers, native
//   addons and worker threads live OUTSIDE the V8 heap. So it is entirely
//   possible to be OOM-killed at 137 with plenty of heap headroom left --
//   `--max-old-space-size` protects a region the kernel does not care about.
//
//     node oom.js --heap    allocate inside the JS heap  -> 134, with a trace
//     node oom.js --buffer  allocate outside it          -> 137, silence
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. The heap limit at the top next to memory.max. Inside
//      `--memory=256m` V8's ceiling is derived from the container, not the
//      host -- compare that with python/oom.py, where nothing is derived.
//   2. Which mode printed something and which did not, and the two exit
//      codes. Record both; you will read them in a restart log one day.
//   3. In --buffer mode: heapUsed stays small while RSS climbs. That is the
//      whole reason --max-old-space-size is not a container memory fix.
//
// RUN
//   docker run --rm --memory=256m -v "$PWD:/w" -w /w node:24-slim node oom.js --heap
//   echo "exit code: $?"      # 134, and a stack trace above it
//   docker run --rm --memory=256m -v "$PWD:/w" -w /w node:24-slim node oom.js --buffer
//   echo "exit code: $?"      # 137, and nothing above it
//
//   node oom.js --heap        # on this Mac: no cgroup, so a self-imposed cap
//
// On macOS there is no cgroup memory controller, so nothing can OOM-kill
// this process. With no limit to read, it imposes its own (--limit-mb) and
// says clearly that it stopped itself. --heap mode still reaches V8's real
// ceiling, because that ceiling exists on every platform.

'use strict';

const fs = require('fs');
const os = require('os');
const v8 = require('v8');

const CHUNK_MB = 8;

// --------------------------------------------------------------- the kernel

function readOrNull(path) {
  try {
    return fs.readFileSync(path, 'utf8').trim();
  } catch {
    return null;
  }
}

function memoryMax() {
  const raw = readOrNull('/sys/fs/cgroup/memory.max');
  if (raw && raw !== 'max') return Number(raw);
  const v1 = readOrNull('/sys/fs/cgroup/memory/memory.limit_in_bytes');
  // v1 spells "unlimited" as a number near 2^63, not as a word.
  if (v1 && Number(v1) < 2 ** 62) return Number(v1);
  return null;
}

function memoryHigh() {
  const raw = readOrNull('/sys/fs/cgroup/memory.high');
  return raw && raw !== 'max' ? Number(raw) : null;
}

function memoryEvents() {
  const raw = readOrNull('/sys/fs/cgroup/memory.events');
  if (!raw) return null;
  const out = {};
  for (const line of raw.split('\n')) {
    const [key, value] = line.split(/\s+/);
    if (key) out[key] = Number(value);
  }
  return out;
}

const mib = (bytes) => (bytes / 2 ** 20).toFixed(0);

function main() {
  const mode = process.argv.includes('--buffer') ? 'buffer' : 'heap';
  const limitArg = process.argv.indexOf('--limit-mb');
  const selfLimitMb = limitArg > -1 ? Number(process.argv[limitArg + 1]) : 512;

  const limit = memoryMax();
  const high = memoryHigh();
  const heapLimit = v8.getHeapStatistics().heap_size_limit;
  const eventsBefore = memoryEvents();

  console.log('7.6 -- memory: Node');
  console.log(`  runtime            : node ${process.version} (V8 ${process.versions.v8}, libuv ${process.versions.uv})`);
  console.log(`  memory.max         : ${limit === null ? 'no limit / no cgroupfs' : mib(limit) + ' MiB'}`);
  console.log(`  memory.high        : ${high === null ? 'unset' : mib(high) + ' MiB'}   <- degrades instead of killing; no Compose key`);
  console.log(`  os.totalmem()      : ${mib(os.totalmem())} MiB   <- the HOST's memory. Not yours`);
  console.log(`  V8 old-space limit : ${mib(heapLimit)} MiB   <- DERIVED from the cgroup, via uv_get_constrained_memory`);
  console.log(`  starting RSS       : ${mib(process.memoryUsage().rss)} MiB`);
  console.log();

  if (limit !== null) {
    const ratio = heapLimit / limit;
    console.log(`  The heap ceiling is ${(ratio * 100).toFixed(0)}% of the container limit.`);
    console.log('  That gap is where Buffers, native addons, worker threads, the code');
    console.log('  cache and libuv itself live -- and none of them are inside the heap');
    console.log('  that --max-old-space-size protects.');
    console.log();
  } else {
    console.log(`  !! No cgroup memory limit on this host, so nothing can OOM-kill this`);
    console.log(`  !! process. In --buffer mode it will stop ITSELF at ${selfLimitMb} MiB and`);
    console.log(`  !! say so; --heap mode still reaches V8's real ceiling, which exists`);
    console.log(`  !! everywhere. For the kill:`);
    console.log(`  !!   docker run --rm --memory=256m -v "$PWD:/w" -w /w node:24-slim node oom.js --buffer`);
    console.log();
  }

  // Every piece of error handling a careful engineer would install. Watch
  // which of them get a chance to run.
  process.on('SIGTERM', () => {
    console.log('  [signal handler] caught SIGTERM -- shutting down cleanly');
    process.exit(143);
  });
  process.on('exit', (code) => {
    console.log(`  [exit hook] final RSS ${mib(process.memoryUsage().rss)} MiB, code ${code}`);
  });
  process.on('uncaughtException', (err) => {
    console.log(`  [uncaughtException] ${err.message}`);
    process.exit(1);
  });
  console.log('  installed: a SIGTERM handler, an exit hook, an uncaughtException hook.');
  console.log('  A cgroup OOM kill runs none of them. Note which lines appear below.');
  console.log();

  const ceilingMb = limit === null ? selfLimitMb : Math.floor((limit / 2 ** 20) * 1.5);
  console.log(`  mode: --${mode}`);
  if (mode === 'heap') {
    console.log('    Allocating inside the JS heap (arrays of numbers). V8 owns this');
    console.log('    region and will abort with a stack trace when it fills.');
  } else {
    console.log('    Allocating Buffers, which live OUTSIDE the V8 heap. V8 has no');
    console.log('    ceiling to enforce here, so the cgroup is the only limit -- and');
    console.log('    the cgroup does not print anything.');
  }
  console.log();

  const blocks = [];
  let allocatedMb = 0;
  while (allocatedMb < ceilingMb) {
    if (mode === 'heap') {
      // A plain Array of small integers, which V8 stores as a FixedArray of
      // SMIs in the OLD SPACE -- so heapUsed climbs and the heap limit is
      // reachable. Deliberately NOT a TypedArray: a Float64Array's backing
      // store is allocated OUTSIDE the V8 heap, so it would leave heapUsed
      // flat and grow `external` instead -- which is --buffer's experiment,
      // not this one. That distinction is the entire point of the two modes,
      // and getting it wrong makes both modes print the same thing.
      blocks.push(new Array((CHUNK_MB * 2 ** 20) / 8).fill(1));
    } else {
      // Buffer.allocUnsafe would not touch the pages. alloc() zero-fills,
      // which is exactly the write the cgroup charges for.
      blocks.push(Buffer.alloc(CHUNK_MB * 2 ** 20, 1));
    }
    allocatedMb += CHUNK_MB;

    if (allocatedMb % 32 === 0 || (limit && allocatedMb * 2 ** 20 > limit * 0.8)) {
      const usage = process.memoryUsage();
      const events = memoryEvents() || {};
      console.log(
        `    allocated ${String(allocatedMb).padStart(5)} MiB` +
        `   RSS ${String(mib(usage.rss)).padStart(6)} MiB` +
        `   heapUsed ${String(mib(usage.heapUsed)).padStart(6)} MiB` +
        `   external ${String(mib(usage.external)).padStart(6)} MiB` +
        `   oom_kill=${events.oom_kill ?? 'n/a'} high=${events.high ?? 'n/a'}`);
    }
  }

  console.log();
  console.log(`  Reached ${allocatedMb} MiB without dying.`);
  if (limit === null) {
    console.log('  Expected: there is no cgroup here to kill anything, and the');
    console.log('  self-imposed ceiling stopped the loop. Nothing was enforced.');
  } else {
    console.log('  NOT expected under a memory limit. The kernel reclaimed enough to');
    console.log('  keep up, or memory.high is set and doing its job -- check the high');
    console.log('  counter above, which is the degrade-instead-of-die signal.');
  }
  console.log();
  console.log('  The two deaths, and how to tell them apart in a restart log:');
  console.log('    exit 134  SIGABRT. V8 printed "Reached heap limit" AND a stack');
  console.log('              trace. Fix: --max-old-space-size, or use less heap.');
  console.log('    exit 137  SIGKILL. Nothing printed, anywhere. Fix: raise');
  console.log('              memory.max, or stop allocating outside the heap.');
  console.log('    Only the second one sets docker inspect .State.OOMKilled = true.');
  if (eventsBefore) {
    console.log();
    console.log(`  memory.events at start: ${JSON.stringify(eventsBefore)}`);
    console.log(`  memory.events now     : ${JSON.stringify(memoryEvents())}`);
  }
}

main();
