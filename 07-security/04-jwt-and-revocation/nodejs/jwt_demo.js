// Layer 7 · Topic 4 — What a JWT is, and the revocation problem (Node.js).
//
// One command, no deps: `node jwt_demo.js`. The README's Node library is
// `jsonwebtoken`, which is not in this machine's offline cache; this file uses
// the built-in `node:crypto` to sign/verify RS256 and HS256 by hand, which
// shows the alg-confusion mechanism MORE directly than the library would. The
// jsonwebtoken lesson still applies: jwt.verify(token, secret) WITHOUT an
// `algorithms` option historically accepted whatever the header said, and
// `secret` is a string|Buffer while an RSA public key is a string too -- so
// the confusion attack is a type-check-passing mistake. The `pinnedVerify`
// below is the `algorithms: ['RS256']` you must pass.
//
// Three parts: (A) a JWT is signed, not encrypted; (B) forge HS256 with the
// RS256 public key as the HMAC secret and watch a naive verifier accept it,
// a pinned verifier reject it; (C) revocation latency per strategy.
//
// What to look for: B's naive verifier prints ACCEPTED (forgery), pinned
// prints REJECTED; C's `plain` latency is ~the remaining TTL, `denylist` ~0.

const crypto = require('node:crypto');

const b64url = (buf) => Buffer.from(buf).toString('base64url');
const enc = (obj) => b64url(JSON.stringify(obj));

function makeRsa() {
  return crypto.generateKeyPairSync('rsa', {
    modulusLength: 2048,
    publicKeyEncoding: { type: 'spki', format: 'pem' },
    privateKeyEncoding: { type: 'pkcs8', format: 'pem' },
  });
}

function signRS256(claims, privateKey) {
  const input = `${enc({ alg: 'RS256', typ: 'JWT' })}.${enc(claims)}`;
  const sig = crypto.sign('RSA-SHA256', Buffer.from(input), privateKey);
  return `${input}.${b64url(sig)}`;
}
function signHS256(claims, secret) {
  const input = `${enc({ alg: 'HS256', typ: 'JWT' })}.${enc(claims)}`;
  const sig = crypto.createHmac('sha256', secret).update(input).digest();
  return `${input}.${b64url(sig)}`;
}

// Naive: trusts header.alg. For HS256 it uses the provided key as an HMAC
// secret -- exactly the old jwt.verify(token, publicKey) trap.
function naiveVerify(token, key) {
  const [h, p, s] = token.split('.');
  const alg = JSON.parse(Buffer.from(h, 'base64url')).alg;
  const input = `${h}.${p}`;
  if (alg === 'RS256') {
    return crypto.verify('RSA-SHA256', Buffer.from(input), key, Buffer.from(s, 'base64url'))
      ? `ACCEPTED role=${JSON.parse(Buffer.from(p, 'base64url')).role}` : 'REJECTED';
  }
  if (alg === 'HS256') {
    const expected = b64url(crypto.createHmac('sha256', key).update(input).digest());
    return crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(s))
      ? `ACCEPTED role=${JSON.parse(Buffer.from(p, 'base64url')).role}` : 'REJECTED';
  }
  return 'REJECTED (unknown alg)';
}

// Pinned: the algorithm is fixed by the verifier, not read from the token.
function pinnedVerify(token, publicKey, alg = 'RS256') {
  const [h, p, s] = token.split('.');
  if (JSON.parse(Buffer.from(h, 'base64url')).alg !== alg) return `REJECTED (alg != ${alg})`;
  return crypto.verify('RSA-SHA256', Buffer.from(`${h}.${p}`), publicKey, Buffer.from(s, 'base64url'))
    ? `ACCEPTED role=${JSON.parse(Buffer.from(p, 'base64url')).role}` : 'REJECTED';
}

function partA() {
  console.log('A. A JWT is signed, NOT encrypted');
  const { privateKey } = makeRsa();
  const token = signRS256({ sub: 'alice', role: 'admin', note: 'not secret' }, privateKey);
  console.log('   claims read with no key:', JSON.parse(Buffer.from(token.split('.')[1], 'base64url')));
  console.log('   -> anyone holding the token reads every claim.\n');
}

function partB() {
  console.log('B. alg-confusion: forge HS256 with the RS256 public key as the secret');
  const { privateKey, publicKey } = makeRsa();
  const good = signRS256({ sub: 'alice', role: 'user' }, privateKey);
  const forged = signHS256({ sub: 'alice', role: 'admin' }, publicKey); // public key AS HMAC secret
  console.log(`   legit RS256, pinned [RS256]:                  ${pinnedVerify(good, publicKey)}`);
  console.log(`   forged HS256, naive verifier (pubkey as key): ${naiveVerify(forged, publicKey)}  <- the attack works`);
  console.log(`   forged HS256, pinned [RS256]:                 ${pinnedVerify(forged, publicKey)}  <- safe`);
  console.log('   The pin fixes the algorithm at the verifier; the token header no\n' +
    '   longer chooses which key type is used. That single option closes it.\n');
}

function partC() {
  console.log('C. Revocation latency by strategy (poll every 50ms after logout)');
  const TTL_MS = 2000, LOGOUT_AT = 500, POLL_MS = 50;
  function latency(strategy) {
    const jti = 'tok-123';
    const denylist = new Set();
    let opaqueDead = false;
    const me = (now) => {
      if (now >= TTL_MS) return 401;
      if (strategy === 'denylist' && denylist.has(jti)) return 401;
      if (strategy === 'opaque_introspect' && opaqueDead) return 401;
      return 200; // plain: /me never consults revocation state
    };
    if (strategy === 'denylist') denylist.add(jti);
    else if (strategy === 'opaque_introspect') opaqueDead = true;
    // plain logout: kills a server session /me does not read
    for (let now = LOGOUT_AT; now <= TTL_MS; now += POLL_MS)
      if (me(now) === 401) return now - LOGOUT_AT;
    return TTL_MS - LOGOUT_AT;
  }
  for (const s of ['plain', 'denylist', 'opaque_introspect']) {
    const ms = latency(s);
    const note = s === 'plain' ? '= full remaining TTL' : '~ one poll interval';
    console.log(`   ${s.padEnd(20)} revocation latency: ${String(ms).padStart(4)} ms   ${note}`);
  }
  console.log("   (plain 'logout' invalidates a session /me never checks)\n");
}

console.log('Layer 7 · Topic 4 — JWT: not-encrypted, alg-confusion, revocation\n');
partA(); partB(); partC();
console.log('Takeaway: a stateless JWT trades revocability for statelessness. ' +
  'Instant logout needs per-request server state (denylist/introspection) -- ' +
  'which is a session by another name.');
