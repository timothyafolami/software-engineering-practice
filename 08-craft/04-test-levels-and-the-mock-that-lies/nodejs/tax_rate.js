// The dependency. Two consumers require it; the test intends to affect one.
//
// `rateFor` is deliberately STRICT about its argument, because that strictness
// is the behaviour a module-level mock deletes. Every bug in this directory is
// a bug in what the callers PASS, and a stub that ignores its arguments cannot
// see any of them.
'use strict';

const TABLE = Object.freeze({
  EU: 0.20,
  UK: 0.20,
  US: 0.00,   // no VAT; sales tax is charged elsewhere, so the rate here is 0
});

function rateFor(region) {
  if (typeof region !== 'string' || !(region in TABLE)) {
    throw new TypeError(`unknown tax region: ${JSON.stringify(region)}`);
  }
  return TABLE[region];
}

module.exports = { rateFor, TABLE };
