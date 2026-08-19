// Layer 10 - Topic 4: the control case. (Node.js)
//
// What this demonstrates
//     JavaScript has exactly one number type, float64, and V8 does not
//     reassociate floating-point expressions. So the naive versions of
//     this topic's experiments mostly DO NOT BREAK here -- and that null
//     result is the lesson, not an absence of one:
//
//       "just use float64" is a genuine mitigation. It is also a 2x
//       bandwidth decision, and topic 1 told you exactly what bandwidth
//       buys at serving time.
//
//     Math.fround() rounds a value to the nearest float32, which lets this
//     file simulate single precision and bring the failure straight back.
//     The two halves of the output are the same program at two precisions.
//
// What to look for
//     - Naive softmax at float64 survives a peak logit of 200 and fails at
//       800; at simulated float32 it fails at 200. Same code, same input.
//     - Partitioned sums: distinct results appear at BOTH precisions --
//       float64 is not associative either -- but compare the relative
//       SPREADS. Orders of magnitude separate "will never cross an argmax
//       boundary" from "will".
//     - The naive-variance row: float64 holds where float32 returned a
//       negative number in python/welford_vs_naive.py.
//
// No dependencies. Runs with no arguments:
//     node nodejs/float64_control.js

'use strict';

const N = 10_000_000;
const SEED = 20260818;

function mulberry32(a) {
  return function rand() {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Math.fround rounds to the nearest float32. Applying it after every
// operation simulates single-precision arithmetic in a language that has
// none, which is the only way to make these failures reappear here.
const f32 = Math.fround;

function bits(x) {
  const buf = new ArrayBuffer(8);
  new Float64Array(buf)[0] = x;
  return `0x${new BigUint64Array(buf)[0].toString(16).padStart(16, '0')}`;
}

function sumPartitioned(data, w, single) {
  const chunk = Math.ceil(data.length / w);
  const partials = [];
  for (let i = 0; i < w; i += 1) {
    const lo = i * chunk;
    if (lo >= data.length) break;
    const hi = Math.min(data.length, lo + chunk);
    let s = 0;
    for (let j = lo; j < hi; j += 1) s = single ? f32(s + data[j]) : s + data[j];
    partials.push(s);
  }
  let total = 0;
  for (const p of partials) total = single ? f32(total + p) : total + p;
  return total;
}

function softmaxNaive(x, single) {
  let total = 0;
  const e = new Array(x.length);
  for (let i = 0; i < x.length; i += 1) {
    e[i] = single ? f32(Math.exp(x[i])) : Math.exp(x[i]);
    total = single ? f32(total + e[i]) : total + e[i];
  }
  return { sum: total / total, maxP: Math.max(...e.map((v) => v / total)) };
}

function softmaxStable(x, single) {
  const m = Math.max(...x);
  let total = 0;
  const e = new Array(x.length);
  for (let i = 0; i < x.length; i += 1) {
    e[i] = single ? f32(Math.exp(f32(x[i] - m))) : Math.exp(x[i] - m);
    total = single ? f32(total + e[i]) : total + e[i];
  }
  return { sum: total / total, maxP: Math.max(...e.map((v) => v / total)) };
}

function naiveVariance(data, single) {
  let sum = 0;
  let sumSq = 0;
  for (const v of data) {
    sum = single ? f32(sum + v) : sum + v;
    sumSq = single ? f32(sumSq + f32(v * v)) : sumSq + v * v;
  }
  const mean = single ? f32(sum / data.length) : sum / data.length;
  const meanSq = single ? f32(sumSq / data.length) : sumSq / data.length;
  return single ? f32(meanSq - f32(mean * mean)) : meanSq - mean * mean;
}

function main() {
  console.log('Node.js - float64 as the control case');
  console.log(`  node ${process.version}, one number type, no reassociation`);
  console.log(`  ${N.toLocaleString()} values ~U(0.5, 1.5), seed ${SEED}\n`);

  const rand = mulberry32(SEED);
  const data = new Float64Array(N);
  for (let i = 0; i < N; i += 1) data[i] = rand() + 0.5;

  console.log('Partitioned summation');
  console.log('-'.repeat(78));
  for (const single of [false, true]) {
    const label = single ? 'simulated float32 (Math.fround)' : 'native float64';
    const seen = new Set();
    const values = [];
    console.log(`  ${label}`);
    console.log(`    ${'workers'.padStart(8)} ${'sum'.padStart(20)} ${'bits'.padStart(20)}`);
    for (const w of [1, 2, 4, 8, 16, 32, 64]) {
      const s = sumPartitioned(data, w, single);
      seen.add(s);
      values.push(s);
      console.log(
        `    ${String(w).padStart(8)} ${s.toFixed(6).padStart(20)} ${bits(s).padStart(20)}`,
      );
    }
    values.sort((a, b) => a - b);
    const spread = (values[values.length - 1] - values[0]) / values[0];
    console.log(
      `    distinct: ${seen.size} of 7    relative spread: ${spread.toExponential(3)}` +
        `    epsilon: ${(single ? 1.1920929e-7 : Number.EPSILON).toExponential(3)}\n`,
    );
  }
  console.log('  float64 addition is not associative either, so distinct results');
  console.log('  appear at both precisions. What differs by orders of magnitude is');
  console.log('  the SPREAD -- and the spread is what decides whether two close');
  console.log('  logits ever swap places.');

  console.log('\nSoftmax, naive vs max-subtracted');
  console.log('-'.repeat(78));
  console.log(
    `  ${'peak'.padStart(6)} ${'precision'.padStart(18)} ${'naive sum'.padStart(12)} ` +
      `${'naive max p'.padStart(13)} ${'stable sum'.padStart(12)} ${'stable max p'.padStart(13)}`,
  );
  const rand2 = mulberry32(7);
  for (const peak of [50, 200, 800]) {
    const x = new Array(1024);
    for (let i = 0; i < x.length; i += 1) {
      // Box-Muller for a normal sample; the shape matters, not the source.
      const u = Math.max(rand2(), 1e-12);
      x[i] = Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * rand2());
    }
    x[0] = peak;
    for (const single of [false, true]) {
      const n = softmaxNaive(x, single);
      const s = softmaxStable(x, single);
      console.log(
        `  ${String(peak).padStart(6)} ${(single ? 'float32 (fround)' : 'float64').padStart(18)} ` +
          `${String(n.sum).padStart(12)} ${n.maxP.toPrecision(6).padStart(13)} ` +
          `${String(s.sum).padStart(12)} ${s.maxP.toPrecision(6).padStart(13)}`,
      );
    }
  }
  console.log('\n  float64 survives a peak of 200 that float32 does not, and fails at');
  console.log('  800 like everything else. Wider formats move the cliff; only the');
  console.log('  max subtraction removes it.');

  console.log('\nNaive variance, E[x^2] - E[x]^2, on N(0,1) + offset');
  console.log('-'.repeat(78));
  console.log(`  ${'offset'.padStart(10)} ${'float64'.padStart(20)} ${'float32 (fround)'.padStart(20)}`);
  const rand3 = mulberry32(11);
  const sample = new Float64Array(200_000);
  for (let i = 0; i < sample.length; i += 1) {
    const u = Math.max(rand3(), 1e-12);
    sample[i] = Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * rand3());
  }
  for (const offset of [0, 1e3, 1e5, 1e6, 1e8]) {
    const shifted = Float64Array.from(sample, (v) => v + offset);
    const d = naiveVariance(shifted, false);
    const f = naiveVariance(Float64Array.from(shifted, f32), true);
    console.log(
      `  ${offset.toExponential(0).padStart(10)} ${d.toFixed(6).padStart(20)} ` +
        `${f.toFixed(6).padStart(20)}`,
    );
  }
  console.log('\n  The true variance is 1.0 in every row. float64 holds far longer,');
  console.log('  and eventually loses too. "Use a wider type" is a real answer with');
  console.log('  a real price, not a way out of thinking about the algorithm --');
  console.log('  python/welford_vs_naive.py is the way out.');
}

main();
