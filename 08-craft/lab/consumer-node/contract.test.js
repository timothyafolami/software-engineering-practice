// Topic 6, the Node consumer's side. `node --test`, no framework to install.
//
// WHAT THIS DEMONSTRATES: the SAME four breaks as consumer-go, and a different
// set of them gets caught -- which is the reason both consumers exist.
import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { loadSnapshot, makeClient, validateAgainstSnapshot } from './client.js';

/** A stub serving what the committed snapshot promises. Never the live API:
 *  running the consumers against the live service is an integration test, and
 *  it will not survive the two services being deployed independently. */
async function stub(body) {
  const server = http.createServer((_req, res) => {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(typeof body === 'string' ? body : JSON.stringify(body));
  });
  await new Promise((r) => server.listen(0, r));
  return { url: `http://127.0.0.1:${server.address().port}`, close: () => server.close() };
}

test('parses the contracted shape', async () => {
  const s = await stub({ items: [{ id: 1, status: 'paid', total_cents: 500 }], total: 1 });
  try {
    const { data } = await makeClient(s.url).fetchOrders(1);
    assert.equal(data.total, 1);
    assert.equal(data.items[0].total_cents, 500);
  } finally { s.close(); }
});

test('break 1: total int -> string passes WITHOUT runtime validation', async () => {
  // The finding, not a broken experiment. The client JSON.parses into a typed
  // variable and never asserts, so TypeScript believes you and the process
  // carries a string where the rest of the code expects a number.
  const s = await stub({ items: [], total: '1' });
  try {
    const { data } = await makeClient(s.url).fetchOrders(1);
    assert.equal(typeof data.total, 'string');
    assert.equal(data.total + 1, '11', 'and now it silently concatenates instead of adding');
  } finally { s.close(); }
});

test('break 1: the SAME response is rejected WITH runtime validation', async () => {
  const s = await stub({ items: [], total: '1' });
  try {
    await assert.rejects(
      () => makeClient(s.url, { validate: true }).fetchOrders(1),
      /`total` is string, contract says integer/,
    );
  } finally { s.close(); }
});

test('break 2: required field made optional is caught only by the validator', async () => {
  const s = await stub({ items: [{ id: 1, status: 'paid', total_cents: 500 }] });
  try {
    const { data } = await makeClient(s.url).fetchOrders(1);
    assert.equal(data.total, undefined, 'unvalidated: undefined propagates silently');

    await assert.rejects(
      () => makeClient(s.url, { validate: true }).fetchOrders(1),
      /missing required field `total`/,
    );
  } finally { s.close(); }
});

test('break 3: a NEW optional field is not breaking for this consumer', async () => {
  const s = await stub({ items: [], total: 0, currency: 'GBP' });
  try {
    const { data } = await makeClient(s.url, { validate: true }).fetchOrders(1);
    assert.equal(data.currency, 'GBP');
  } finally { s.close(); }
});

test('the committed contract still declares every field this consumer reads', () => {
  const snapshot = loadSnapshot();
  for (const [name, fields] of [
    ['CustomerOrderListOut', ['items', 'total']],
    ['CustomerOrderOut', ['id', 'status', 'total_cents']],
  ]) {
    const required = new Set(snapshot.components.schemas[name].required ?? []);
    for (const f of fields) {
      assert.ok(required.has(f), `${name}.${f} is no longer required in the contract`);
    }
  }
});

test('validateAgainstSnapshot names the field, the way a compiler would', () => {
  const snapshot = loadSnapshot();
  assert.throws(
    () => validateAgainstSnapshot(snapshot, 'CustomerOrderOut', { id: 1, status: 'paid' }),
    /missing required field `total_cents`/,
  );
});
