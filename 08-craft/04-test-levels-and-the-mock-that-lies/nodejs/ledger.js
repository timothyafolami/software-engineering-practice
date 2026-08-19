// The consumer nobody was thinking about. Bug 2 lives here.
//
// This module is not the subject of any test in this directory. It is pulled in
// transitively because `postAudit` is what records the money, and it requires
// the SAME `tax_rate` module -- so a module-registry substitution aimed at
// `quote.js` lands here too.
//
// BUG 2: it lowercases the region before looking the rate up. `TABLE` has no
// lowercase keys, so the real `rateFor` throws. The stub does not.
'use strict';

const { rateFor } = require('./tax_rate');

const ROWS = [];

function postAudit(order) {
  const subtotal = order.lines.reduce((n, l) => n + l.unitCents * l.qty, 0);
  const rate = rateFor(String(order.regionCode).toLowerCase());  // BUG 2
  const row = { orderId: order.id, subtotal, taxCents: Math.round(subtotal * rate) };
  ROWS.push(row);
  return row;
}

function rows() { return ROWS.slice(); }

module.exports = { postAudit, rows };
