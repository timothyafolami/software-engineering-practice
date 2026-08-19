// Layer 2 · Topic 7 - Node's contribution to the SYN table.
//
// fetch() goes through undici's global dispatcher, which keeps a per-origin
// pool. Nothing is configured here on purpose: this measures the DEFAULT,
// which is the thing Topic 1 claimed makes Node the runtime least likely to
// have the cold-client bug.
//
//   LAB_URL=http://127.0.0.1:8000/work node syn_client.js
'use strict';

const URL_ = process.env.LAB_URL || 'http://127.0.0.1:8000/work';
const N = Number(process.env.LAB_REQUESTS || 30);

(async () => {
  const t0 = Date.now();
  for (let i = 0; i < N; i += 1) {
    const res = await fetch(URL_);
    await res.arrayBuffer();
  }
  console.log(`global fetch dispatcher, ${N} requests in ${Date.now() - t0} ms`);
  process.exit(0);
})();
