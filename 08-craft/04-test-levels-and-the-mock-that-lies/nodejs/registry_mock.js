// jest.mock / vi.mock, reduced to the twelve lines that actually do the work.
//
// Neither jest nor vitest is installed here and neither is needed: the whole
// mechanism is a write into the CommonJS module registry. `jest.mock(spec, fn)`
// resolves `spec`, puts a fabricated module object into the registry under that
// resolved filename, and every `require` of that file thereafter -- from ANY
// module, including ones you have never heard of -- receives the stub.
//
// The part people know is that babel-jest HOISTS `jest.mock(...)` above the
// `require`/`import` statements. The part that matters is WHY that hoisting is
// load-bearing, which `demonstrateHoisting()` below shows: a module that has
// already been required has already destructured the real function, and a stub
// installed afterwards reaches nobody. Hoisting exists to get the stub in
// before the graph is built -- which is the same property that makes it reach
// consumers you never named.
'use strict';

const Module = require('module');

/** Put `exportsObj` into the registry under an already-resolved filename. */
function installStub(resolvedId, exportsObj) {
  const stub = new Module(resolvedId, null);
  stub.filename = resolvedId;
  stub.loaded = true;
  stub.exports = exportsObj;
  require.cache[resolvedId] = stub;
}

/** Drop modules from the registry so the next require re-executes them. */
function evict(...resolvedIds) {
  for (const id of resolvedIds) delete require.cache[id];
}

/**
 * A stub that answers every call with `returns` and records what it was asked.
 * It ignores its arguments, which is the entire failure mode of this directory.
 */
function recordingRate(returns) {
  const calls = [];
  return {
    calls,
    exports: {
      rateFor(...args) { calls.push(args); return returns; },
      TABLE: {},
    },
  };
}

module.exports = { installStub, evict, recordingRate };
