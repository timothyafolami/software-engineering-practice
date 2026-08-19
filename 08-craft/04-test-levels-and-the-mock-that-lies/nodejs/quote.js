// The module the test MEANT to test. Bug 1 lives here.
//
// BUG 1: the order field is `regionCode`; this reads `order.region`, which is
// always `undefined`. Against the real `tax_rate` that throws immediately.
// Against a stub that ignores its argument it returns a plausible number, and
// the test is green.
'use strict';

const { rateFor } = require('./tax_rate');

function quoteTotalCents(order) {
  const subtotal = order.lines.reduce((n, l) => n + l.unitCents * l.qty, 0);
  const rate = rateFor(order.region);          // BUG 1: field is `regionCode`
  return subtotal + Math.round(subtotal * rate);
}

module.exports = { quoteTotalCents };
