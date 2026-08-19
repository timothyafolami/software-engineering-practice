// Layer 1 - The fix: worker_threads gives the busy loop its own OS thread
// entirely, so the main thread's event loop (and the ticker on it) is
// never blocked. This is the mechanism behind libuv's thread pool, which
// is what fs.readFile, crypto.pbkdf2, and DNS lookups actually use under
// the hood instead of blocking your main thread.

const { Worker } = require("worker_threads");

const TICK_INTERVAL = 100;
const BLOCK_DURATION = 1000;
const LEAD_IN = 200;
const LEAD_OUT = 200;

let ticks = 0;
const start = Date.now();
const interval = setInterval(() => {
  ticks++;
}, TICK_INTERVAL);

const workerSrc = `
  const { workerData, parentPort } = require('worker_threads');
  const until = Date.now() + workerData.ms;
  while (Date.now() < until) { /* busy loop, but on its own OS thread */ }
  parentPort.postMessage('done');
`;

setTimeout(() => {
  const w = new Worker(workerSrc, { eval: true, workerData: { ms: BLOCK_DURATION } });
  w.once("message", () => w.terminate());
}, LEAD_IN);

setTimeout(() => {
  clearInterval(interval);
  const elapsed = (Date.now() - start) / 1000;
  const expected = elapsed / (TICK_INTERVAL / 1000);
  console.log(
    `[good] ticks counted: ${ticks}  over ${elapsed.toFixed(2)}s  ` +
      `(expected ~${expected.toFixed(0)} if the ticker were never blocked)`
  );
}, LEAD_IN + BLOCK_DURATION + LEAD_OUT + 50);
