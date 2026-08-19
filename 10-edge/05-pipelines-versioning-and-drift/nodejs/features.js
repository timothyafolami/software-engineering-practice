// Layer 10 - Topic 5: the third implementation, in the shape it usually
// arrives in production. (Node.js)
//
// What this demonstrates
//     A small feature computed in the frontend BFF "because it was easier
//     there". Nobody decided to fork the transform; somebody needed one
//     number in a response and forty lines looked cheaper than a service
//     call. Now three implementations exist, each individually reasonable,
//     and they produce three different answers.
//
//     This file's native mode differs from python/features.py's spec in
//     three places, and NONE of them overlap with the three that
//     golang/features.go gets wrong -- which is the actual lesson. The
//     divergences are not a shared misunderstanding you could fix with one
//     memo. They are independent decisions, so they compose:
//
//       window boundary  both ends inclusive, [T-7d, T]. The spec is
//                        half-open. The Go implementation chose (T-7d, T].
//                        Three readings, three answers.
//       rounding         Math.round is half-UP for positive values (and
//                        asymmetric for negatives, which is its own bug
//                        waiting for a refund feature). The spec is
//                        half-to-even; Go's math.Round is half-away-from-
//                        zero. Three languages, three defaults.
//       recency          Math.round of the day difference instead of
//                        floor, so an event 1.6 days old reports as 2.
//
//     Then `--mode conform` implements the written spec exactly and the
//     diff goes to zero -- which is the evidence for the real fix. The
//     transform gets ONE HOME and everything else calls it across a
//     boundary. A third correct copy is not a fix, it is a third thing to
//     keep correct.
//
// What to look for
//     - Run both modes and diff with python3 python/three_way_diff.py.
//     - Note that the Python/Node disagreement count is NOT the same as
//       the Python/Go one, and the Go/Node one is larger than either.
//       Skew compounds; it does not average out.
//
// No dependencies. Reads the same events.csv every implementation reads:
//     node nodejs/features.js
//     node nodejs/features.js --mode conform --out data/features_node_conform.csv

'use strict';

const fs = require('node:fs');
const path = require('node:path');

const DAY_MS = 86_400_000;
const WINDOW_MS = 7 * DAY_MS;
const AS_OF_MS = 1_785_542_400_000; // 2026-08-01T00:00:00Z

function parseArgs(argv) {
  const args = { mode: 'native', in: null, out: null };
  for (let i = 2; i < argv.length; i += 2) {
    const key = argv[i].replace(/^--/, '');
    args[key] = argv[i + 1];
  }
  const root = path.resolve(__dirname, '..');
  args.in ??= path.join(root, 'data', 'events.csv');
  args.out ??= path.join(
    root,
    'data',
    args.mode === 'conform' ? 'features_node_conform.csv' : 'features_node_native.csv',
  );
  return args;
}

/** Round-half-to-even, which JavaScript does not provide. */
function roundHalfEven(value, places) {
  const scale = 10 ** places;
  const scaled = value * scale;
  const floor = Math.floor(scaled);
  const diff = scaled - floor;
  let rounded;
  if (diff > 0.5) rounded = floor + 1;
  else if (diff < 0.5) rounded = floor;
  else rounded = floor % 2 === 0 ? floor : floor + 1;
  return rounded / scale;
}

/** What an unhurried JavaScript developer reaches for. Half-up. */
function roundNative(value, places) {
  const scale = 10 ** places;
  return Math.round(value * scale) / scale;
}

function loadEvents(file) {
  const lines = fs.readFileSync(file, 'utf8').trim().split('\n');
  const events = [];
  for (let i = 1; i < lines.length; i += 1) {
    const [userId, tsMs, amount] = lines[i].split(',');
    events.push({
      userId: Number(userId),
      tsMs: Number(tsMs),
      amount: Number(amount),
    });
  }
  return events;
}

function compute(events, asOf, conform) {
  const byUser = new Map();
  for (const e of events) {
    if (!byUser.has(e.userId)) byUser.set(e.userId, []);
    byUser.get(e.userId).push(e);
  }
  const lower = asOf - WINDOW_MS;
  const ids = [...byUser.keys()].sort((a, b) => a - b);
  const out = [];
  for (const id of ids) {
    let spend = 0;
    let count = 0;
    let latest = -1;
    for (const e of byUser.get(id)) {
      const inWindow = conform
        ? e.tsMs >= lower && e.tsMs < asOf // the spec: half-open
        : e.tsMs >= lower && e.tsMs <= asOf; // "the last seven days, inclusive"
      if (!inWindow) continue;
      spend += e.amount;
      count += 1;
      if (e.tsMs > latest) latest = e.tsMs;
    }
    let avg = 0;
    let recency = -1;
    if (count > 0) {
      const raw = spend / count;
      avg = conform ? roundHalfEven(raw, 2) : roundNative(raw, 2);
      const days = (asOf - latest) / DAY_MS;
      recency = conform ? Math.floor(days) : Math.round(days);
    } else if (!conform) {
      recency = -1; // this one Node happens to get right
    }
    out.push({ userId: id, spend, count, avg, recency });
  }
  return out;
}

function main() {
  const args = parseArgs(process.argv);
  const conform = args.mode === 'conform';

  let events;
  try {
    events = loadEvents(args.in);
  } catch (err) {
    console.error(`cannot read ${args.in}: ${err.message}`);
    console.error('run python3 python/seed_events.py first');
    process.exit(1);
  }

  const rows = compute(events, AS_OF_MS, conform);
  const lines = ['user_id,spend_7d,txn_count_7d,avg_amount_7d,recency_days'];
  for (const r of rows) {
    lines.push(`${r.userId},${r.spend},${r.count},${r.avg.toFixed(2)},${r.recency}`);
  }
  fs.writeFileSync(args.out, `${lines.join('\n')}\n`);

  console.log(`Node.js feature implementation (${args.mode} mode)`);
  console.log(`  input  : ${args.in} (${events.length} events)`);
  console.log(`  output : ${args.out} (${rows.length} users)`);
  if (conform) {
    console.log('\n  This mode implements python/features.py\'s docstring exactly:');
    console.log('  half-open window, round-half-to-even, floor for recency.');
    console.log('  The three-way diff against Python should be zero rows.');
  } else {
    console.log('\n  This mode is the bug, written the way it really happens:');
    console.log('    window    [T-7d, T]  instead of [T-7d, T)');
    console.log('    rounding  Math.round (half up)');
    console.log('    recency   Math.round instead of floor');
    console.log('  Note that none of these are the three Go gets wrong. That is');
    console.log('  the point: independent decisions compose rather than cancel.');
  }
  console.log('\n  Next: python3 python/three_way_diff.py');
}

main();
