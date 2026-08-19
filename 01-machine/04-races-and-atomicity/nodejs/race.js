// Layer 1 - `i++` is not atomic, demonstrated with the only thing in Node
// that gives you a genuine hardware-level data race: SharedArrayBuffer
// across real OS threads (worker_threads). Your everyday single-threaded
// Node code never has this problem for its own variables -- there's only
// one thread touching them -- but the moment you reach for
// SharedArrayBuffer + workers for parallelism, you're in the same world as
// Go and Rust, races included.
const { Worker, isMainThread, workerData, parentPort } = require("worker_threads");

const THREADS = 8;
const INCREMENTS = 300_000;

if (isMainThread) {
  async function run(useAtomics) {
    const sab = new SharedArrayBuffer(4);
    const view = new Int32Array(sab);
    Atomics.store(view, 0, 0);

    const workers = Array.from({ length: THREADS }, () => {
      return new Promise((resolve) => {
        const w = new Worker(__filename, { workerData: { sab, useAtomics } });
        w.once("exit", resolve);
      });
    });
    await Promise.all(workers);
    return Atomics.load(view, 0);
  }

  (async () => {
    const expected = THREADS * INCREMENTS;
    const unsafeResult = await run(false);
    const safeResult = await run(true);
    console.log(`expected:                 ${expected}`);
    console.log(`unsafe (view[0]++):       ${unsafeResult}  (lost ${expected - unsafeResult})`);
    console.log(`safe (Atomics.add):       ${safeResult}`);
  })();
} else {
  const view = new Int32Array(workerData.sab);
  if (workerData.useAtomics) {
    for (let i = 0; i < INCREMENTS; i++) {
      Atomics.add(view, 0, 1);
    }
  } else {
    for (let i = 0; i < INCREMENTS; i++) {
      // Not atomic: a plain JS read-modify-write on shared memory.
      view[0] = view[0] + 1;
    }
  }
  parentPort.close();
}
