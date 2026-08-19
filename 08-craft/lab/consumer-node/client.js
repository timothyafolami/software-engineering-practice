// The Node consumer, with BOTH halves of the story in one file.
//
// WHAT THIS DEMONSTRATES: `fetchOrders` is the ordinary version -- parse the
// JSON into a variable the types describe and get on with it. It cannot fail on
// a contract break, because types erase at runtime. `fetchOrdersValidated` adds
// the runtime assertion built FROM THE SAME SNAPSHOT, which is the thing that
// actually makes a generated client a contract consumer.
//
// WHAT TO LOOK FOR: `validateAgainstSnapshot` reads the committed
// openapi.snapshot.json at startup rather than hard-coding the field list. That
// is what makes it a contract check instead of a second, drifting copy of the
// contract.
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));

const SNAPSHOT_CANDIDATES = [
  resolve(HERE, '../api/openapi.snapshot.json'),
  resolve(HERE, 'api/openapi.snapshot.json'),
  '/work/api/openapi.snapshot.json',
];

export function loadSnapshot() {
  for (const p of SNAPSHOT_CANDIDATES) {
    try { return JSON.parse(readFileSync(p, 'utf8')); } catch { /* try the next */ }
  }
  throw new Error(`committed contract not found; looked in ${SNAPSHOT_CANDIDATES.join(', ')}`);
}

/** Required-field and type expectations, derived from the contract at runtime. */
export function schemaExpectations(snapshot, name) {
  const schema = snapshot.components?.schemas?.[name];
  if (!schema) throw new Error(`the contract no longer declares ${name}`);
  const required = new Set(schema.required ?? []);
  const types = Object.fromEntries(
    Object.entries(schema.properties ?? {}).map(([k, v]) => [k, v.type ?? 'unknown']),
  );
  return { required, types };
}

const JSON_TYPE = (v) =>
  Array.isArray(v) ? 'array' : v === null ? 'null' : typeof v === 'number'
    ? (Number.isInteger(v) ? 'integer' : 'number') : typeof v;

/** Throws a message naming the field, which is what a build failure would have done. */
export function validateAgainstSnapshot(snapshot, name, value) {
  const { required, types } = schemaExpectations(snapshot, name);
  const problems = [];
  for (const field of required) {
    if (!(field in value)) problems.push(`missing required field \`${field}\``);
  }
  for (const [field, expected] of Object.entries(types)) {
    if (!(field in value)) continue;
    const actual = JSON_TYPE(value[field]);
    const ok = expected === actual
      || (expected === 'number' && actual === 'integer')
      || (expected === 'integer' && actual === 'integer');
    if (!ok) problems.push(`\`${field}\` is ${actual}, contract says ${expected}`);
  }
  if (problems.length) {
    throw new Error(`${name} violates the committed contract: ${problems.join('; ')}`);
  }
  return value;
}

export function makeClient(base, { validate = false } = {}) {
  const snapshot = validate ? loadSnapshot() : null;

  return {
    /**
     * THE ORDINARY VERSION. Structurally satisfied by anything.
     * Returns { data, queueMs } so ladder F can report where the wait was.
     */
    async fetchOrders(customerId, { signal } = {}) {
      const started = performance.now();
      // undici's connect/headers/body timeouts are explicit and separate; the
      // one nobody sets is the connection-acquisition wait, which is exactly
      // the queue that hides during topic 7's incident.
      const res = await fetch(`${base}/customers/${customerId}/orders?limit=50`, { signal });
      const queueMs = Math.round(performance.now() - started);
      if (!res.ok) {
        const body = await res.json().catch(() => ({ error: 'unknown', message: '' }));
        const err = new Error(`api ${res.status}: ${body.error} ${body.message}`);
        err.status = res.status;
        err.queueMs = queueMs;
        throw err;
      }
      const data = await res.json();
      // No assertion. TypeScript believed the annotation; nothing checked it.
      if (validate) validateAgainstSnapshot(snapshot, 'CustomerOrderListOut', data);
      return { data, queueMs };
    },
  };
}
