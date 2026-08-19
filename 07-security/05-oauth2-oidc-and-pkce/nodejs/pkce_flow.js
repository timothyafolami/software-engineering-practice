// Layer 7 · Topic 5 — OAuth2, OIDC, and PKCE end to end (Node.js).
//
// One command, no deps, no network: `node pkce_flow.js`. The protocol is HTTP
// redirects and one SHA-256; the whole flow is modelled in-process by a
// minimal AuthServer and a scripted attacker who intercepts the authorization
// code from the redirect Location. The README's Node library is
// `openid-client`, whose callback API takes the expected state and nonce as
// ARGUMENTS -- a check you must pass a value to is harder to skip than a check
// you must call. Here the "check" is the AS comparing the PKCE verifier.
//
// Each scenario configures the AuthServer and tries to redeem an intercepted
// code; the measurement is tokens issued (0 or 1). The load-bearing pair is
// replay-WRONG-verifier above replay-no-verifier: generating a verifier proves
// nothing, comparing it is the control.

const crypto = require('node:crypto');

const b64url = (buf) => Buffer.from(buf).toString('base64url');
const s256 = (verifier) => b64url(crypto.createHash('sha256').update(verifier).digest());
const rnd = (n = 24) => crypto.randomBytes(n).toString('base64url');

class AuthServer {
  constructor({ pkceMode = 'required', codeTtl = 60, singleUse = true, redirectMatch = 'exact' } = {}) {
    this.pkceMode = pkceMode;
    this.codeTtl = codeTtl;
    this.singleUse = singleUse;
    this.redirectMatch = redirectMatch;
    this.registeredRedirect = 'https://app.test/cb';
    this.codes = new Map();
    this.now = 0;
  }
  authorize(redirectUri, state, codeChallenge = null, method = 'S256') {
    const code = rnd(16);
    this.codes.set(code, { challenge: codeChallenge, method, redirectUri, issuedAt: this.now, used: false });
    return { code, state };
  }
  redirectOk(presented) {
    return this.redirectMatch === 'exact'
      ? presented === this.registeredRedirect
      : presented.startsWith(this.registeredRedirect); // the prefix bug
  }
  token(code, codeVerifier = null, redirectUri = null) {
    const rec = this.codes.get(code);
    if (!rec) return [400, null];
    if (this.now - rec.issuedAt > this.codeTtl) return [400, null];        // expired
    if (rec.used && this.singleUse) return [400, null];                    // replay
    if (!this.redirectOk(redirectUri)) return [400, null];                 // redirect mismatch
    if (this.pkceMode === 'required' || (this.pkceMode === 'optional' && rec.challenge)) {
      if (!codeVerifier) return [400, null];                               // required but absent
      const got = rec.method === 'S256' ? s256(codeVerifier) : codeVerifier;
      if (got !== rec.challenge) return [400, null];                       // THE control
    }
    rec.used = true;
    return [200, 'access-token-' + rnd(8)];
  }
}

function scenario(name, cfg, run) {
  const [issued, detail] = run(new AuthServer(cfg));
  console.log(`   ${name.padEnd(22)} tokens issued: ${issued}   ${detail}`);
}

console.log('Layer 7 · Topic 5 — OAuth2 / PKCE: replay, downgrade, reuse, redirect\n');
console.log('  Attacker holds an intercepted authorization code and tries to redeem it:');

scenario('happy-path', { pkceMode: 'required' }, (a) => {
  const v = rnd(32);
  const { code } = a.authorize('https://app.test/cb', 'xyz', s256(v));
  const [, tok] = a.token(code, v, 'https://app.test/cb');
  return [tok ? 1 : 0, 'legit client with the matching verifier -> OK'];
});
scenario('replay-no-verifier', { pkceMode: 'required' }, (a) => {
  const v = rnd(32);
  const { code } = a.authorize('https://app.test/cb', 'xyz', s256(v));
  const [, tok] = a.token(code, null, 'https://app.test/cb');
  return [tok ? 1 : 0, 'no code_verifier -> AS must refuse'];
});
scenario('replay-wrong-verifier', { pkceMode: 'required' }, (a) => {
  const v = rnd(32);
  const { code } = a.authorize('https://app.test/cb', 'xyz', s256(v));
  const [, tok] = a.token(code, rnd(32), 'https://app.test/cb');
  return [tok ? 1 : 0, 'wrong verifier -> proves the AS VERIFIES'];
});
scenario('replay-no-pkce', { pkceMode: 'off' }, (a) => {
  const { code } = a.authorize('https://app.test/cb', 'xyz', null);
  const [, tok] = a.token(code, null, 'https://app.test/cb');
  return [tok ? 1 : 0, 'PKCE off -> a stolen code alone is enough'];
});
scenario('downgrade-plain', { pkceMode: 'optional' }, (a) => {
  const secret = 'attacker-chosen-value';
  const { code } = a.authorize('https://app.test/cb', 'xyz', secret, 'plain');
  const [, tok] = a.token(code, secret, 'https://app.test/cb');
  return [tok ? 1 : 0, 'method=plain lets the attacker pick both halves'];
});
scenario('code-reuse', { pkceMode: 'required', singleUse: true }, (a) => {
  const v = rnd(32);
  const { code } = a.authorize('https://app.test/cb', 'xyz', s256(v));
  a.token(code, v, 'https://app.test/cb');
  const [, tok] = a.token(code, v, 'https://app.test/cb');
  return [tok ? 1 : 0, 'second use of a single-use code -> refused'];
});
scenario('code-expiry', { pkceMode: 'required', codeTtl: 60 }, (a) => {
  const v = rnd(32);
  const { code } = a.authorize('https://app.test/cb', 'xyz', s256(v));
  a.now = 61;
  const [, tok] = a.token(code, v, 'https://app.test/cb');
  return [tok ? 1 : 0, 'redeemed after 61s (ttl=60) -> expired'];
});
const redirectRun = (a) => {
  const v = rnd(32);
  const { code } = a.authorize('https://app.test/cb.attacker.test', 'xyz', s256(v));
  const [, tok] = a.token(code, v, 'https://app.test/cb.attacker.test');
  return [tok ? 1 : 0, `redeem cb.attacker.test under ${a.redirectMatch} matching`];
};
scenario('redirect-prefix', { pkceMode: 'required', redirectMatch: 'prefix' }, redirectRun);
scenario('redirect-exact ', { pkceMode: 'required', redirectMatch: 'exact' }, redirectRun);

console.log('\nRead: PKCE (S256) makes an intercepted code useless without the ' +
  'verifier the attacker never saw -- but only if the AS COMPARES it ' +
  '(replay-wrong-verifier must be 0) and refuses plain downgrades. Single-use, ' +
  'short TTL and exact redirect matching close the rest. Each "1" is a homegrown-AS bug.');
