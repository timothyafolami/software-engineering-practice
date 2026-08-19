// Layer 1 - Memory & cache locality
// Same pointer-chasing benchmark as the Python version: two physical layouts
// of the same logical traversal, using typed arrays so we're actually
// looking at raw memory access patterns rather than boxed-object overhead.
//
// sequential -> node i's successor lives at i+1 (cache-friendly)
// shuffled   -> node i's successor is a random other node (cache-hostile)

const N = 2_000_000;
const LAPS = 5;

function build(shuffled) {
  const values = new Int32Array(N);
  const nextIdx = new Int32Array(N);
  for (let i = 0; i < N; i++) values[i] = i;

  if (!shuffled) {
    for (let i = 0; i < N; i++) nextIdx[i] = (i + 1) % N;
  } else {
    const perm = new Int32Array(N);
    for (let i = 0; i < N; i++) perm[i] = i;
    // Fisher-Yates
    for (let i = N - 1; i > 0; i--) {
      const j = (Math.random() * (i + 1)) | 0;
      const tmp = perm[i];
      perm[i] = perm[j];
      perm[j] = tmp;
    }
    for (let i = 0; i < N; i++) {
      nextIdx[perm[i]] = perm[(i + 1) % N];
    }
  }
  return { values, nextIdx };
}

function traverse(values, nextIdx, laps) {
  let total = 0;
  let idx = 0;
  const steps = N * laps;
  for (let s = 0; s < steps; s++) {
    total += values[idx];
    idx = nextIdx[idx];
  }
  return total;
}

function bench(label, shuffled) {
  const { values, nextIdx } = build(shuffled);
  const start = process.hrtime.bigint();
  const total = traverse(values, nextIdx, LAPS);
  const elapsedNs = Number(process.hrtime.bigint() - start);
  const elapsedS = elapsedNs / 1e9;
  const nsPerStep = elapsedNs / (N * LAPS);
  console.log(
    `${label.padEnd(10)}  total=${String(total).padStart(15)}  time=${elapsedS.toFixed(3)}s  ${nsPerStep.toFixed(1)} ns/step`
  );
}

console.log(`N=${N.toLocaleString()} laps=${LAPS} (node ${process.version})`);
bench("sequential", false);
bench("shuffled", true);
