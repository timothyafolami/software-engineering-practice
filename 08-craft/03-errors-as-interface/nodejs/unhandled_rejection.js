// Layer 8 Topic 3 - Node: the "fix" that converts a loud crash into a silent failure.
//
// WHAT THIS DEMONSTRATES: two Node-specific hazards, both of which turn a
// category-3 bug into a category-nothing.
//
//   1. A promise rejected with no handler TERMINATES the process on modern Node.
//      That is CORRECT category-3 behaviour, and it regularly gets "fixed" by
//      installing a global `unhandledRejection` handler that logs and continues.
//   2. An `async` function's rejection is invisible unless someone awaits it, so
//      fire-and-forget work in a handler swallows its own errors by construction.
//
// WHAT TO LOOK FOR: the BROKEN run reports success while the audit row was never
// written. The FIXED run either writes it or reports a failure -- there is no
// third outcome. `new Error(msg, { cause })` is Node's `raise ... from`.
//
//   node nodejs/unhandled_rejection.js

class Unavailable extends Error {
  constructor(dep, retryAfter, options) {
    super(`${dep} unavailable`, options);
    this.name = 'Unavailable';
    this.dep = dep;
    this.retryAfter = retryAfter;
  }
}

const auditLog = [];

async function writeAuditRow(orderId) {
  await new Promise((r) => setTimeout(r, 5));
  // The dependency is down. This is category 2 and the caller could retry.
  throw new Unavailable('audit-store', 2, { cause: new Error('ECONNREFUSED 10.0.0.7:5432') });
}

// --- BROKEN -----------------------------------------------------------------

async function placeOrderBROKEN(orderId) {
  // Fire and forget. The `async` call returns a promise nobody holds, so its
  // rejection is invisible to this function and to its caller. The handler
  // returns 201 and the audit row does not exist.
  writeAuditRow(orderId);
  return { status: 201, orderId };
}

// --- FIXED ------------------------------------------------------------------

async function placeOrderFIXED(orderId) {
  try {
    await writeAuditRow(orderId);
  } catch (err) {
    if (err instanceof Unavailable) {
      // Translate, preserving the cause. The caller gets something it can act on.
      const e = new Error('order accepted but not audited', { cause: err });
      e.status = 503;
      e.retryAfter = err.retryAfter;
      throw e;
    }
    throw err; // not ours to interpret -- let it reach the top and crash loudly
  }
  return { status: 201, orderId };
}

function chain(err) {
  const parts = [];
  for (let e = err; e; e = e.cause) parts.push(`${e.name}: ${e.message}`);
  return parts.join('\n              caused by  ');
}

async function main() {
  console.log('=== BROKEN: fire-and-forget rejection ===');
  const res = await placeOrderBROKEN(1);
  console.log(' ', JSON.stringify(res), '<- 201, and the caller believes it');
  // Give the orphaned promise time to reject so the process-level listener sees it.
  await new Promise((r) => setTimeout(r, 20));
  console.log(`  audit rows written: ${auditLog.length}   <- the row does not exist`);

  console.log('\n=== FIXED: awaited, translated, cause preserved ===');
  try {
    await placeOrderFIXED(2);
  } catch (err) {
    console.log(`  status ${err.status}, Retry-After ${err.retryAfter}s`);
    console.log(`  ${chain(err)}`);
    console.log('  -> three links: what the caller sees, what we decided, what actually');
    console.log('     happened. Only the first is in the response body.');
  }

  console.log('\n=== the global handler that turns a crash into a silence ===');
  console.log('  process.on("unhandledRejection", (e) => logger.warn(e))');
  console.log('  looks like hygiene and is the Node spelling of `except Exception: pass`.');
  console.log('  Modern Node ALREADY does the right thing: it terminates. Installing');
  console.log('  that listener replaces a discoverable crash with a warning nobody');
  console.log('  reads, on a process that keeps serving traffic in an unknown state.');
  console.log('  If you must install one, make it log AND re-raise or exit non-zero.');
}

// This is the shape to copy: observe, then still die. It is the only version
// that keeps the crash discoverable while giving you a chance to say why.
process.on('unhandledRejection', (reason) => {
  console.log(`  [unhandledRejection] ${reason?.name}: ${reason?.message}`);
  console.log('  [unhandledRejection] a correct handler logs and then exits non-zero.');
  console.log('  [unhandledRejection] this demo continues only so the FIXED half can run.');
});

main();
