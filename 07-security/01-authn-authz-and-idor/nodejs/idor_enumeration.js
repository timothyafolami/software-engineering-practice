// Layer 7 · Topic 1 — IDOR as a missing authorization check (Node.js).
//
// One command, no arguments, no deps: `node idor_enumeration.js`.
// Seeds 1,500 invoices across three interleaved tenants, logs in as alice,
// and fires the same 1,000-request enumeration at three handler variants,
// counting per variant how many 200 responses carried an owner that is NOT
// alice. That count is the leak per 1,000 requests.
//
// Node's specific hazard, called out in the README, is middleware ordering:
// `app.use(requireAuth)` on one router feels like coverage, and a route on a
// different router silently has no check. We model that directly: the
// "vulnerable" handler is authenticated (we know it's alice) but never
// authorizes the object. Prisma's findUnique({ where: { id } }) is the exact
// analogue of store.get(id) below -- a primary-key lookup with no caller.
//
// What to look for: vulnerable leaks ~667/1000; both fixes leak 0 and answer
// a wrong-owner id with 404 (not 403), so an enumerator learns nothing.

const N_INVOICES = 1500;
const CALLER = 1; // alice
const N_REQUESTS = 1000;

class InvoiceStore {
  constructor() {
    this.rows = new Map();
    for (let i = 1; i <= N_INVOICES; i++) {
      this.rows.set(i, { id: i, ownerId: ((i - 1) % 3) + 1, amount: i * 100 });
    }
    this.requestUser = null; // the RLS "session variable"
  }
  get(id) {
    // findUnique({ where: { id } }) -- no notion of a caller.
    return this.rows.get(id) ?? null;
  }
  getScoped(id, ownerId) {
    const r = this.rows.get(id);
    return r && r.ownerId === ownerId ? r : null;
  }
  setRequestUser(ownerId) { this.requestUser = ownerId; }
  getRls(id) {
    const r = this.rows.get(id);
    if (!r || this.requestUser === null) return null;
    return r.ownerId === this.requestUser ? r : null;
  }
}

const handlerVulnerable = (store, caller, id) => {
  const r = store.get(id);
  return r ? [200, r] : [404, null];
};
const handlerFixedQuery = (store, caller, id) => {
  const r = store.getScoped(id, caller);
  return r ? [200, r] : [404, null];
};
const handlerFixedRls = (store, caller, id) => {
  store.setRequestUser(caller); // SET LOCAL app.current_user
  const r = store.getRls(id);
  return r ? [200, r] : [404, null];
};

function* enumerationIds(n) {
  // Deterministic LCG: reproducible, measured numbers (not chosen ones).
  let x = 1;
  for (let i = 0; i < n; i++) {
    x = (1103515245 * x + 12345) % 0x80000000;
    yield 1 + (x % N_INVOICES);
  }
}

function run(handler, label) {
  const store = new InvoiceStore();
  let leaked = 0, own = 0, notFound = 0;
  for (const id of enumerationIds(N_REQUESTS)) {
    const [status, row] = handler(store, CALLER, id);
    if (status === 200) (row.ownerId !== CALLER ? leaked++ : own++);
    else notFound++;
  }
  const [wrongStatus] = handler(store, CALLER, 2); // id 2 is bob's
  console.log(
    `  ${label.padEnd(14)} leaked=${String(leaked).padStart(4)}/${N_REQUESTS}   ` +
    `own=${String(own).padStart(4)}  not_found=${String(notFound).padStart(4)}   ` +
    `wrong-owner id -> HTTP ${wrongStatus}`
  );
}

console.log("Layer 7 · Topic 1 — IDOR enumeration (logged in as alice, id=1)");
console.log(`seed: ${N_INVOICES} invoices, 3 tenants interleaved, alice owns ${N_INVOICES / 3 | 0}`);
console.log(`attack: ${N_REQUESTS} requests, ids uniform over 1..${N_INVOICES}\n`);
console.log("  handler        wrong-owner rows leaked per 1,000 requests");
run(handlerVulnerable, "vulnerable");
run(handlerFixedQuery, "fixed_query");
run(handlerFixedRls, "fixed_rls");
console.log("\nRead: authentication (we KNOW it's alice) is not authorization " +
  "(may alice touch THIS object). The vulnerable handler has the first and " +
  "skips the second; the fixes push the check into the fetch, where it cannot " +
  "return a row alice does not own.");
