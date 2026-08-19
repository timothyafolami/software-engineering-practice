// Layer 1 - What a syscall actually costs.
// Same read(/dev/zero) vs pure-loop comparison as the Python version.

const fs = require("fs");

const N = 500_000;

function benchSyscall() {
  const fd = fs.openSync("/dev/zero", "r");
  const buf = Buffer.alloc(1);
  const start = process.hrtime.bigint();
  for (let i = 0; i < N; i++) {
    fs.readSync(fd, buf, 0, 1, null);
  }
  const elapsed = Number(process.hrtime.bigint() - start) / 1e9;
  fs.closeSync(fd);
  return elapsed;
}

function benchPureJs() {
  let total = 0;
  const start = process.hrtime.bigint();
  for (let i = 0; i < N; i++) {
    total += i & 0xff;
  }
  const elapsed = Number(process.hrtime.bigint() - start) / 1e9;
  return elapsed;
}

const tSys = benchSyscall();
const tPure = benchPureJs();
console.log(`N=${N.toLocaleString()}`);
console.log(`read(/dev/zero) x${N}:  ${tSys.toFixed(3)}s  (${((tSys / N) * 1e9).toFixed(1)} ns/call)`);
console.log(`pure JS loop:           ${tPure.toFixed(3)}s  (${((tPure / N) * 1e9).toFixed(1)} ns/iter)`);
console.log(`syscall is ${(tSys / tPure).toFixed(1)}x the cost of an equivalent pure-JS step`);
