// Layer 1 - Same experiment as the Python version, from Node's side. Node
// has no direct getrlimit() binding in the standard library, so we read
// /proc/self/limits directly (Linux-specific, which is fine for this
// sandbox) to report the ceiling we're about to hit.
const fs = require("fs");

function readSoftLimit() {
  const limits = fs.readFileSync("/proc/self/limits", "utf8");
  const line = limits.split("\n").find((l) => l.startsWith("Max open files"));
  return line ? line.trim() : "(unknown -- not on Linux?)";
}

console.log(readSoftLimit());

const fds = [];
try {
  while (true) {
    fds.push(fs.openSync("/dev/null", "r"));
  }
} catch (e) {
  console.log(`hit ${e.code} ('too many open files') after opening ${fds.length} fds`);
} finally {
  for (const fd of fds) fs.closeSync(fd);
  console.log(`closed all ${fds.length} fds; process is healthy again`);
}
