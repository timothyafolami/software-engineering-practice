// Layer 1 - Thread ("worker") vs process creation cost.
// Node's user code is single-threaded, but worker_threads gives you real OS
// threads for JS execution. This times spawn+exit for a worker_thread
// versus spawn+exit for a full child process.

const { Worker } = require("worker_threads");
const { fork } = require("child_process");
const path = require("path");

const N = 100; // both are slower to spin up in Node than in Go/Rust; keep small

const workerScript = `
const { parentPort } = require('worker_threads');
parentPort.postMessage('done');
`;

function spawnWorker() {
  return new Promise((resolve) => {
    const w = new Worker(workerScript, { eval: true });
    w.once("message", () => w.terminate().then(resolve));
  });
}

function spawnChildProcess() {
  return new Promise((resolve) => {
    const child = fork(path.join(__dirname, "noop_child.js"));
    child.once("exit", resolve);
  });
}

async function benchWorkers() {
  const start = process.hrtime.bigint();
  for (let i = 0; i < N; i++) {
    await spawnWorker();
  }
  return Number(process.hrtime.bigint() - start) / 1e9;
}

async function benchProcesses() {
  const start = process.hrtime.bigint();
  for (let i = 0; i < N; i++) {
    await spawnChildProcess();
  }
  return Number(process.hrtime.bigint() - start) / 1e9;
}

(async () => {
  const tWorker = await benchWorkers();
  const tProc = await benchProcesses();
  console.log(`N=${N}`);
  console.log(`worker_thread spawn+exit:  ${tWorker.toFixed(3)}s  (${((tWorker / N) * 1e6).toFixed(1)} us/worker)`);
  console.log(`child process spawn+exit:  ${tProc.toFixed(3)}s  (${((tProc / N) * 1e6).toFixed(1)} us/process)`);
  console.log(`process is ${(tProc / tWorker).toFixed(1)}x the cost of a worker_thread`);
})();
