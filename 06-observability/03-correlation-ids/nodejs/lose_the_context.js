/*
 * Layer 6 Topic 3 - Losing trace context at a Node.js concurrency boundary.
 *
 * What this demonstrates
 * ----------------------
 * Node's answer to in-process context is `AsyncLocalStorage`, built on
 * async_hooks. It is the most automatic of the six: promises, timers, I/O
 * callbacks and `await` all keep the store, because the runtime tracks the
 * async resource that scheduled the continuation.
 *
 * It loses context in exactly one shape, and this file is that shape: a
 * library that manages its OWN callback queue and invokes your callback from
 * a different async resource than the one that registered it. Old connection
 * pools, hand-rolled event emitters and batching clients all do this. The
 * symptom in production is that one integration silently starts a new trace
 * while every other part of the service is fine -- so it reads as "that
 * library is weird" rather than as a propagation bug.
 *
 * The fix is `AsyncResource.bind`, which captures the current async context at
 * bind time and restores it when the function is later called from anywhere.
 * That is what a well-behaved library does for you, and what an old one does
 * not.
 *
 * No OpenTelemetry SDK is imported -- none is installed on this machine. The
 * span, the W3C `traceparent` codec and the leaky pool are all here in the
 * file, because the failure belongs to the runtime and not to an SDK.
 *
 * What to look for in the output
 * ------------------------------
 * Four blocks in one shape:
 *
 *   caller trace_id   <id>
 *   callee trace_id   <id or "none">   naive
 *   callee trace_id   <id>             propagated
 *   verdict           lost | preserved
 *
 * The setTimeout/promise block is the control: preserved, with no work from
 * you. The pool block is the same program losing the same store, because the
 * callback is fired from a timer that was registered outside any request.
 * Then read the last section: the ONE thread running every request means a
 * lost store is not a per-request accident, it is whatever the event loop
 * happened to be doing at the time.
 */

'use strict';

const { AsyncLocalStorage, AsyncResource } = require('node:async_hooks');
const crypto = require('node:crypto');

// ---------------------------------------------------------------------------
// A minimal span + W3C traceparent codec: the entire cross-process half.
// ---------------------------------------------------------------------------

function makeSpan(name, traceId) {
  return {
    name,
    traceId: traceId || crypto.randomBytes(16).toString('hex'),
    spanId: crypto.randomBytes(8).toString('hex'),
    sampled: true,
  };
}

function traceparentOf(span) {
  // version-traceid-spanid-flags
  return `00-${span.traceId}-${span.spanId}-${span.sampled ? '01' : '00'}`;
}

function spanFromTraceparent(header, name) {
  const [version, traceId, parentId, flags] = header.split('-');
  if (version !== '00' || traceId.length !== 32 || parentId.length !== 16) {
    throw new Error(`malformed traceparent: ${header}`);
  }
  const span = makeSpan(name, traceId);
  span.sampled = (parseInt(flags, 16) & 1) === 1;
  return span;
}

const als = new AsyncLocalStorage();
const currentTraceId = () => (als.getStore() ? als.getStore().traceId : 'none');

// ---------------------------------------------------------------------------
// Structured logging: read the store PER RECORD. A logger that reads it once
// at module load stamps every line with the same id, which looks like it works
// right up until you query by one.
// ---------------------------------------------------------------------------

const capturedLogs = [];
function log(msg) {
  capturedLogs.push({ msg, trace_id: als.getStore() ? als.getStore().traceId : '' });
}

function report(boundary, caller, naive, propagated, note) {
  const verdict = naive === caller ? 'preserved' : 'lost';
  console.log(`boundary          ${boundary}`);
  console.log(`caller trace_id   ${caller}`);
  console.log(`callee trace_id   ${naive.padEnd(32)} naive`);
  console.log(`callee trace_id   ${propagated.padEnd(32)} propagated`);
  console.log(`verdict           ${verdict}${note ? `   (${note})` : ''}`);
  console.log();
  return verdict;
}

// ---------------------------------------------------------------------------
// Boundary 1: await / setTimeout / promise chain -- the control.
// ---------------------------------------------------------------------------

async function boundaryAwait() {
  const span = makeSpan('GET /orders');
  return als.run(span, async () => {
    await new Promise((resolve) => setTimeout(resolve, 5));
    const afterTimer = currentTraceId();
    const afterPromise = await Promise.resolve().then(() => currentTraceId());
    const observed = afterTimer === afterPromise ? afterTimer : 'DISAGREE';
    return report(
      'await / setTimeout / .then',
      span.traceId,
      observed,
      observed,
      'async_hooks follows the continuation; nothing to fix',
    );
  });
}

// ---------------------------------------------------------------------------
// Boundary 2: a library with its own callback queue. This is the leaky shape.
//
// LeakyPool stores your callback in an array and fires it later from a timer
// that IT registered, at module scope, before your request existed. The
// callback runs inside the timer's async context, not yours.
// ---------------------------------------------------------------------------

class LeakyPool {
  constructor() {
    this.pending = [];
    // Registered once, outside any request. This is the async resource that
    // will end up invoking every callback.
    this.drainer = setInterval(() => this.drain(), 5);
  }

  query(sql, cb) {
    this.pending.push({ sql, cb });
  }

  drain() {
    const batch = this.pending.splice(0, this.pending.length);
    for (const { sql, cb } of batch) cb(null, { sql, rows: 1 });
  }

  close() {
    clearInterval(this.drainer);
  }
}

function boundaryPool(pool) {
  const span = makeSpan('GET /orders');
  return als.run(span, () => new Promise((resolve) => {
    let naive = null;
    let propagated = null;

    // Naive: hand the pool a bare closure.
    pool.query('SELECT 1', () => {
      log('pool callback (naive)');
      naive = currentTraceId();
      maybeDone();
    });

    // Propagated: bind the callback to the CURRENT async context first. This
    // is precisely what a modern client library does on your behalf.
    pool.query('SELECT 1', AsyncResource.bind(() => {
      log('pool callback (bound)');
      propagated = currentTraceId();
      maybeDone();
    }));

    function maybeDone() {
      if (naive === null || propagated === null) return;
      resolve(report(
        'pool with its own callback queue',
        span.traceId,
        naive,
        propagated,
        'fix = AsyncResource.bind(cb) at the point you hand it over',
      ));
    }
  }));
}

// ---------------------------------------------------------------------------
// Boundary 3: a queue. No async resource is involved at all -- the job is a
// row in a table. Only the message body can carry the context.
// ---------------------------------------------------------------------------

function boundaryQueue() {
  const span = makeSpan('POST /orders');
  return als.run(span, () => {
    const jobs = [
      { id: 'naive', customer: 'cust-0042' },
      { id: 'propagated', customer: 'cust-0042', traceparent: traceparentOf(span) },
    ];

    const seen = {};
    for (const job of jobs) {
      // The consumer is a different process in the lab. Here it is a function
      // called with no ambient context, which is the same thing.
      const consumed = als.run(undefined, () => {
        if (job.traceparent) {
          return als.run(spanFromTraceparent(job.traceparent, 'job'), () => {
            log(`processing job ${job.id}`);
            return currentTraceId();
          });
        }
        log(`processing job ${job.id}`);
        return currentTraceId();
      });
      seen[job.id] = consumed;
    }

    return report(
      'Postgres-backed queue',
      span.traceId,
      seen.naive,
      seen.propagated,
      'the transport carries no headers; put traceparent in the body',
    );
  });
}

// ---------------------------------------------------------------------------
// Boundary 4: the outbound HTTP call -- the easy half, made concrete.
// ---------------------------------------------------------------------------

function boundaryHttp() {
  const span = makeSpan('GET /orders');
  const header = traceparentOf(span);
  const downstream = spanFromTraceparent(header, 'GET /price');
  console.log('boundary          HTTP request to pricing');
  console.log(`caller trace_id   ${span.traceId}`);
  console.log(`traceparent sent  ${header}`);
  console.log(`callee trace_id   ${downstream.traceId.padEnd(32)} parsed from the header`);
  console.log('verdict           preserved   (this is what being a W3C standard buys)');
  console.log();
  return 'preserved';
}

// ---------------------------------------------------------------------------
// The Node-specific closing point: interleaving. Two requests are in flight on
// ONE thread. With the store, each callback still knows which request it
// belongs to; without it, "the current request" is a global that the last
// arrival overwrote.
// ---------------------------------------------------------------------------

let ambientGlobal = null; // the thing people write instead of AsyncLocalStorage

async function interleaved() {
  const results = [];

  async function handle(name, delayMs) {
    const span = makeSpan(name);
    ambientGlobal = span; // the naive "current request" variable
    return als.run(span, async () => {
      await new Promise((r) => setTimeout(r, delayMs));
      results.push({
        request: name,
        withStore: currentTraceId(),
        withGlobal: ambientGlobal.traceId,
        correct: span.traceId,
      });
    });
  }

  await Promise.all([handle('req-A', 30), handle('req-B', 5)]);

  console.log('--- Two requests interleaved on one thread ---');
  for (const r of results) {
    const storeOk = r.withStore === r.correct ? 'ok' : 'WRONG';
    const globalOk = r.withGlobal === r.correct ? 'ok' : 'WRONG';
    console.log(`  ${r.request}  AsyncLocalStorage=${storeOk}   module-global=${globalOk}`);
  }
  console.log('  req-B finished first and overwrote the module-global, so req-A');
  console.log('  attributes its own work to req-B. Not a truncated trace: a');
  console.log('  complete, confident, wrong one.');
  console.log();
}

async function main() {
  console.log('Layer 6 Topic 3 - losing trace context in Node.js (AsyncLocalStorage)');
  console.log(`node ${process.version}   ${process.platform}/${process.arch}`);
  console.log('='.repeat(72));
  console.log();

  const verdicts = {};
  verdicts['await / timers'] = await boundaryAwait();

  const pool = new LeakyPool();
  verdicts['pool callback queue'] = await boundaryPool(pool);
  pool.close();

  verdicts['queue'] = boundaryQueue();
  verdicts['http traceparent'] = boundaryHttp();

  await interleaved();

  console.log('--- Summary: which boundaries Node covers for you ---');
  for (const [name, verdict] of Object.entries(verdicts)) {
    const who = verdict === 'preserved' ? 'runtime carries it' : 'YOU carry it';
    console.log(`  ${name.padEnd(22)} ${verdict.padEnd(10)} ${who}`);
  }
  console.log();

  console.log('--- The one-query test, on the log lines this run emitted ---');
  const withId = capturedLogs.filter((r) => r.trace_id).length;
  console.log(`  log lines emitted            ${capturedLogs.length}`);
  console.log(`  lines carrying a trace_id    ${withId}`);
  console.log(`  lines carrying nothing       ${capturedLogs.length - withId}   <- unqueryable by request`);
  for (const rec of capturedLogs) {
    console.log(`    ${rec.msg.padEnd(28)} trace_id=${rec.trace_id || '(empty)'}`);
  }
}

main();
