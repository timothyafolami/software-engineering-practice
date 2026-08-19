/*
 * Layer 6 Topic 1 - What one unit of telemetry costs the process emitting it.
 *
 * Why Node: it is the strictest version of the bill. Python pays for telemetry
 * on a thread that at least *could* have been one of several; Node pays for it
 * on the one thread that runs every concurrent request in the process. A
 * microsecond spent formatting a log line is a microsecond during which no
 * other request in flight can make progress, so here the ns/op column is not a
 * CPU-budget number, it is a concurrency-capacity number.
 *
 * The second Node-specific trap, and the reason this file writes to a counting
 * sink instead of stdout: when Node's stdout is a TTY or a file it writes
 * *synchronously*. `console.log` in a request handler is a blocking write on
 * the event loop thread. Piped stdout is async. The same code therefore has
 * different latency depending on how the process was started, which is why
 * production Node logs through a buffered transport and never through console.
 *
 * What this demonstrates
 * ----------------------
 * Five operations, timed on this machine:
 *   1. counter add      - Map lookup on a bounded label key + increment
 *   2. span record      - object allocation, hrtime timestamps, six attributes
 *   3. log line (INFO)  - JSON.stringify + write to the sink
 *   4. debug, DISABLED, template literal built eagerly    <- the bug
 *   5. debug, DISABLED, guarded by a level check          <- the fix
 *
 * Operations 4 and 5 emit nothing. The difference between them is pure waste,
 * paid per request, forever.
 *
 * This measures the shape of the cost with a hand-rolled metric/span/log
 * store, not the OpenTelemetry SDK (not installed here). A real SDK adds work
 * on top of these numbers; it never subtracts any.
 *
 * What to look for in the output
 * ------------------------------
 * - The counter-to-log-line ratio. It is not 2x.
 * - Row 4 versus row 5: identical output (none), very different cost.
 * - The warm-up line. V8 tiers up; the first thousand iterations of anything
 *   are interpreted, so a benchmark without warm-up measures the compiler.
 *
 * Run:  node signal_cost.js
 */

'use strict';

const ITERATIONS = 200_000;
const WARMUP = 20_000;

// Printed at the end so V8 cannot decide the work below is dead.
let sink = 0n;

class CounterStore {
  constructor() {
    this.series = new Map();
  }
  add(key, value = 1) {
    this.series.set(key, (this.series.get(key) || 0) + value);
  }
}

class Span {
  constructor(name, traceId, spanId, attributes) {
    this.name = name;
    this.traceId = traceId;
    this.spanId = spanId;
    this.attributes = attributes;
    this.startNs = process.hrtime.bigint();
    this.endNs = 0n;
  }
  end() {
    this.endNs = process.hrtime.bigint();
  }
}

// Stands in for the pipe to the log shipper: counts bytes, discards them.
class CountingSink {
  constructor() {
    this.bytesWritten = 0;
    this.lines = 0;
  }
  write(line) {
    this.bytesWritten += Buffer.byteLength(line);
    this.lines += 1;
  }
}

// A logger of the shape every Node logging library has: a numeric level, and
// methods that check it. pino and winston both look like this underneath.
const LEVELS = { debug: 20, info: 30, warn: 40, error: 50 };

class Logger {
  constructor(sinkStream, level) {
    this.sink = sinkStream;
    this.level = LEVELS[level];
  }
  isEnabled(level) {
    return LEVELS[level] >= this.level;
  }
  debug(line) {
    if (LEVELS.debug >= this.level) this.sink.write(line + '\n');
  }
  info(line) {
    if (LEVELS.info >= this.level) this.sink.write(line + '\n');
  }
}

function bench(label, fn) {
  for (let i = 0; i < WARMUP; i++) fn(); // let V8 tier up before timing
  const start = process.hrtime.bigint();
  for (let i = 0; i < ITERATIONS; i++) fn();
  const elapsed = process.hrtime.bigint() - start;
  return { label, nsPerOp: Number(elapsed) / ITERATIONS };
}

function main() {
  const sinkStream = new CountingSink();
  const logger = new Logger(sinkStream, 'info'); // debug is disabled
  const counter = new CounterStore();

  const order = {
    order_id: 'ord_8f31c2',
    customer_id: 'cus_00194',
    items: [{ sku: 'SKU-1', qty: 2 }, { sku: 'SKU-7', qty: 1 }],
    discount: 0.15,
    currency: 'GBP',
  };
  const labelKey = 'GET|/orders/{id}|200';
  const attributes = {
    'http.request.method': 'GET',
    'http.route': '/orders/{id}',
    'http.response.status_code': 200,
    'db.system.name': 'postgresql',
    'customer.id': order.customer_id,
    'order.id': order.order_id,
  };

  const ops = [
    bench('counter.add (3 bounded labels)', () => {
      counter.add(labelKey);
    }),
    bench('span create + end (6 attrs)', () => {
      const span = new Span('GET /orders/{id}', '4bf92f3577b34da6a3ce929d0e0e4736',
        '00f067aa0ba902b7', attributes);
      span.end();
      sink += span.endNs - span.startNs;
    }),
    bench('log INFO, one JSON line', () => {
      logger.info(JSON.stringify({
        level: 'info',
        msg: 'order priced',
        order_id: order.order_id,
        customer_id: order.customer_id,
        duration_ms: 12.4,
      }));
    }),
    bench('log DEBUG (disabled), eager template literal', () => {
      // THE BUG. logger.level is info, so nothing is written -- but the
      // template literal, and therefore JSON.stringify(order), is evaluated
      // before `debug` is even called. Arguments are evaluated at the call
      // site in JavaScript; there is no lazy form of a template literal.
      logger.debug(`pricing payload=${JSON.stringify(order)}`);
    }),
    bench('log DEBUG (disabled), level-check guard', () => {
      // THE FIX. Same shape as pino's `logger.isLevelEnabled('debug')`.
      if (logger.isEnabled('debug')) {
        logger.debug(`pricing payload=${JSON.stringify(order)}`);
      }
    }),
  ];

  const line = '='.repeat(74);
  console.log(line);
  console.log(`COST OF EMITTING ONE UNIT OF TELEMETRY   (Node ${process.version}, n=${ITERATIONS})`);
  console.log(line);
  console.log('operation'.padEnd(46) + 'ns/op'.padStart(12));
  for (const op of ops) {
    console.log(op.label.padEnd(46) + op.nsPerOp.toFixed(0).padStart(12));
  }

  const eager = ops[3].nsPerOp;
  const guarded = ops[4].nsPerOp;
  console.log(`\nRows 4 and 5 both emit nothing. Row 4 costs ${(eager - guarded).toFixed(0)} ns more`);
  console.log('than row 5 for exactly zero output. At 8 disabled debug calls per');
  console.log(`request and 1000 req/s that is ${((8 * 1000 * (eager - guarded)) / 1e6).toFixed(1)} ms/s of event-loop time`);
  console.log('spent producing nothing -- and event-loop time is the resource every');
  console.log('other in-flight request is waiting for.');

  const perRequest = ops[0].nsPerOp + 3 * ops[1].nsPerOp + 2 * ops[2].nsPerOp + 8 * ops[3].nsPerOp;
  const perRequestFixed = ops[0].nsPerOp + 3 * ops[1].nsPerOp + 2 * ops[2].nsPerOp + 8 * ops[4].nsPerOp;
  console.log('\nOne request = 1 counter + 3 spans + 2 INFO logs + 8 disabled debug logs');
  console.log(`  as written : ${(perRequest / 1000).toFixed(1)} us of event loop per request`);
  console.log(`  with guards: ${(perRequestFixed / 1000).toFixed(1)} us of event loop per request`);
  console.log(`  ceiling on requests/sec from telemetry alone, as written: ${(1e9 / perRequest).toFixed(0)}`);
  console.log('  (that ceiling assumes the handler itself costs nothing, which is the');
  console.log('   point -- telemetry sets a hard cap on a single-threaded runtime.)');

  console.log(`\nBytes written by the INFO logs: ${sinkStream.bytesWritten} over ${sinkStream.lines} lines`
    + ` (${(sinkStream.bytesWritten / sinkStream.lines).toFixed(0)} B/line).`);
  console.log(`(sink=${sink}, printed so nothing above can be optimised away)`);
}

main();
