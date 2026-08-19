// Layer 1 - Node's concurrency model: one thread runs your JS, period.
// setInterval schedules the ticker on that same thread. A synchronous
// busy loop (standing in for JSON.parse on a huge payload, a synchronous
// crypto op, bcrypt.hashSync, anything CPU-bound) never yields back to the
// event loop, so nothing else -- not the ticker, not any other request
// this process is handling -- runs until it returns.

const TICK_INTERVAL = 100;
const BLOCK_DURATION = 1000;
const LEAD_IN = 200;
const LEAD_OUT = 200;

let ticks = 0;
const start = Date.now();
const interval = setInterval(() => {
  ticks++;
}, TICK_INTERVAL);

function blockingWork(ms) {
  const until = Date.now() + ms;
  while (Date.now() < until) {
    /* busy-wait: this occupies the one JS thread, on purpose */
  }
}

setTimeout(() => {
  blockingWork(BLOCK_DURATION);
}, LEAD_IN);

setTimeout(() => {
  clearInterval(interval);
  const elapsed = (Date.now() - start) / 1000;
  const expected = elapsed / (TICK_INTERVAL / 1000);
  console.log(
    `[bad] ticks counted: ${ticks}  over ${elapsed.toFixed(2)}s  ` +
      `(expected ~${expected.toFixed(0)} if the ticker were never blocked)`
  );
}, LEAD_IN + BLOCK_DURATION + LEAD_OUT + 50);
