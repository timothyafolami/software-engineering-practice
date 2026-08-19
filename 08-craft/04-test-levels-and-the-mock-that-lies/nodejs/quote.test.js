// Layer 8 Topic 4 - Node: the module-level mock, and the consumer you did not name.
//
// WHAT THIS DEMONSTRATES: `jest.mock` / `vi.mock` substitute a module for EVERY
// consumer in the graph, not for the one you were testing. This file plants two
// bugs -- `quote.js` reads the wrong field name, `ledger.js` lowercases a
// lookup key -- and both are invisible, because one stub covers both call sites
// and the stub ignores its arguments.
//
// Nothing here is jest. `registry_mock.js` is the twelve lines of registry
// writing that jest.mock compiles down to, so the mechanism is readable rather
// than framework magic. Stdlib only: `node:test` and `node:assert`.
//
// WHAT TO LOOK FOR: every test passes. Then read the two DIAGNOSTIC lines the
// last test prints -- they are the real `rateFor` refusing both call sites, one
// of which belongs to a module this test file never mentions.
//
//   cd nodejs && node --test
'use strict';

const test = require('node:test');
const assert = require('node:assert');

const { installStub, evict, recordingRate } = require('./registry_mock');

const TAX = require.resolve('./tax_rate');
const QUOTE = require.resolve('./quote');
const LEDGER = require.resolve('./ledger');

const ORDER = {
  id: 'ord-1',
  regionCode: 'EU',              // note the field name. `quote.js` reads `region`.
  lines: [{ unitCents: 1000, qty: 2 }],
};

/** Install the stub, then build the graph on top of it -- i.e. hoisting. */
function withStub(rate) {
  evict(TAX, QUOTE, LEDGER);
  const stub = recordingRate(rate);
  installStub(TAX, stub.exports);
  return { stub, quote: require('./quote'), ledger: require('./ledger') };
}

function withRealModules() {
  evict(TAX, QUOTE, LEDGER);
  return { quote: require('./quote'), ledger: require('./ledger') };
}

test('quote total includes VAT', () => {
  const { quote } = withStub(0.20);
  // 2000 subtotal + 20% = 2400. Correct arithmetic on a rate nobody looked up.
  assert.strictEqual(quote.quoteTotalCents(ORDER), 2400);
});

test('quote consults the tax table exactly once', (t) => {
  const { stub, quote } = withStub(0.20);
  quote.quoteTotalCents(ORDER);

  // The count-only assertion. This is `assert_called_once()`, and it is green.
  assert.strictEqual(stub.calls.length, 1);

  // The same run, asked the question the assertion above cannot ask. This is
  // `assert_called_once_with(...)`, and it is the cheap mitigation the topic
  // recommends: the argument is `undefined`, because `quote.js` read a field
  // that does not exist on the order.
  assert.deepStrictEqual(stub.calls[0], [undefined]);
  t.diagnostic(`rateFor was called with [${String(stub.calls[0][0])}] -- `
    + 'assert_called_once() passes on this, assert_called_once_with("EU") does not');
});

test('the audit row is written', (t) => {
  // This test names `ledger`, but no test in this file ever asked for `ledger`
  // to be mocked. It got the stub anyway, because the substitution is on the
  // DEPENDENCY, and the dependency is shared.
  const { stub, ledger } = withStub(0.20);
  const row = ledger.postAudit(ORDER);

  assert.strictEqual(row.orderId, 'ord-1');
  assert.strictEqual(row.subtotal, 2000);
  assert.strictEqual(row.taxCents, 400);          // plausible, and never computed
  t.diagnostic(`ledger asked for rate ${JSON.stringify(stub.calls[0])} -- `
    + 'a lowercase key the real table does not contain');
});

test('hoisting is why the stub reaches modules you did not name', (t) => {
  // Install the stub AFTER the graph is built. `quote.js` destructured the real
  // `rateFor` at require time, so it holds a direct reference and the registry
  // write reaches nobody.
  evict(TAX, QUOTE, LEDGER);
  require('./quote');
  const stub = recordingRate(0.20);
  installStub(TAX, stub.exports);

  assert.throws(() => require('./quote').quoteTotalCents(ORDER), TypeError);
  assert.strictEqual(stub.calls.length, 0);
  t.diagnostic('stub installed after require: 0 calls recorded, real module still in force. '
    + 'That is the problem babel-jest hoisting solves -- and solving it is what '
    + 'makes the substitution global.');
});

test('EVIDENCE: both call sites are rejected by the real module', (t) => {
  const { quote, ledger } = withRealModules();

  const messageOf = (fn) => { try { fn(); return '(no error -- unexpected)'; } catch (e) { return e.message; } };
  const quoteMsg = messageOf(() => quote.quoteTotalCents(ORDER));
  const ledgerMsg = messageOf(() => ledger.postAudit(ORDER));

  assert.match(quoteMsg, /unknown tax region: undefined/);
  assert.match(ledgerMsg, /unknown tax region: "eu"/);

  t.diagnostic(`DIAGNOSTIC quote.js  -> ${quoteMsg}`);
  t.diagnostic(`DIAGNOSTIC ledger.js -> ${ledgerMsg}`);
  t.diagnostic('Four green tests above. Two modules that cannot execute. '
    + 'The stub supplied a rate for both, and asserted-on nothing that would '
    + 'have distinguished a looked-up rate from an invented one.');
});
