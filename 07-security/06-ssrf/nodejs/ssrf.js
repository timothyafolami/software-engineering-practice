// Layer 7 · Topic 6 — SSRF: validate the connection, not the string (Node.js).
//
// One command, no deps, no network: `node ssrf.js`.
// The README's Node path: undici's Agent accepts a custom `connect` option --
// the correct place to pin, because you get the hostname and return a socket,
// so validation and connection happen in the same step with no window between
// them. `fetch` follows redirects by DEFAULT (redirect: 'follow'); 'manual' is
// the right setting for a fetcher. Node has no built-in private-range check, so
// this file implements one -- which is exactly the code a real `connect` hook
// needs.
//
// The finding: string_blocklist ALLOWs every internal target via an encoding
// it did not enumerate; resolve_and_pin BLOCKs them by checking the resolved IP.

const FAKE_DNS = {
  'internal-admin': '10.7.0.10',
  metadata: '10.7.0.169',
  'allowed.test': '93.184.216.34',
  'a.rebind.lab.test': '10.7.0.10',
  localhost: '127.0.0.1',
};
const STRING_DENY = ['localhost', '127.0.0.1', '169.254.169.254'];
const PAYLOADS = [
  ['http://internal-admin:8000/secrets', 'plain internal reach'],
  ['http://10.7.0.169/latest/meta-data/iam/...', 'cloud metadata / credential theft'],
  ['http://0/secrets', '0 == 0.0.0.0 (this host)'],
  ['http://2130706433/', 'decimal form of 127.0.0.1'],
  ['http://[::1]:8000/', 'IPv6 loopback'],
  ['http://ok.test@10.7.0.10/secrets', 'userinfo confusion (real host after @)'],
  ['http://a.rebind.lab.test/secrets', 'DNS rebinding (TOCTOU)'],
];

function hostOf(raw) {
  try {
    const h = new URL(raw).hostname;      // drops userinfo
    return h.replace(/^\[|\]$/g, '');      // strip IPv6 brackets
  } catch { return ''; }
}

function canonicalIP(host) {
  if (FAKE_DNS[host]) host = FAKE_DNS[host];
  if (/^\d+$/.test(host)) {                // "0", "2130706433"
    const n = Number(host) >>> 0;
    return `${(n >>> 24) & 255}.${(n >>> 16) & 255}.${(n >>> 8) & 255}.${n & 255}`;
  }
  if (/^\d+\.\d+\.\d+\.\d+$/.test(host) || host.includes(':')) return host;
  return null; // unknown name
}

function isDenied(ip) {
  if (ip.includes(':')) { // IPv6
    const l = ip.toLowerCase();
    return l === '::1' || l === '::' || l.startsWith('fe80') || l.startsWith('fc') || l.startsWith('fd');
  }
  const [a, b] = ip.split('.').map(Number);
  return a === 127 || a === 10 || a === 0 ||
    (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && b === 168) ||
    (a === 169 && b === 254);
}

function verdictBlocklist(raw) {
  // A real naive blocklist runs over the URL TEXT as sent. (Note: new URL()
  // would canonicalize the decimal IP to 127.0.0.1 and accidentally catch it
  // -- an interesting WHATWG-parser nuance, but not what a string filter does.)
  const low = raw.toLowerCase();
  return STRING_DENY.some((d) => low.includes(d)) ? 'BLOCK' : 'ALLOW';
}
function verdictResolvePin(raw) {
  const ip = canonicalIP(hostOf(raw));
  if (ip === null) return ['BLOCK', 'unresolvable'];
  return [isDenied(ip) ? 'BLOCK' : 'ALLOW', ip];
}

console.log('Layer 7 · Topic 6 — SSRF: string blocklist vs resolve-and-pin\n');
console.log(`   ${'payload'.padEnd(44)}${'blocklist'.padEnd(11)}${'resolve+pin'.padEnd(13)}resolved`);
let rb = 0, rp = 0;
for (const [url] of PAYLOADS) {
  const v1 = verdictBlocklist(url);
  const [v2, ip] = verdictResolvePin(url);
  if (v1 === 'ALLOW') rb++;
  if (v2 === 'ALLOW') rp++;
  console.log(`   ${url.padEnd(44)}${v1.padEnd(11)}${v2.padEnd(13)}${ip}`);
}
console.log(`\n   internal targets reached -- string_blocklist: ${rb}/${PAYLOADS.length}   resolve_and_pin: ${rp}/${PAYLOADS.length}`);
console.log('\nIMDS v1 vs v2: v1 returns credentials to a plain GET; v2 refuses a GET');
console.log('without a PUT-obtained token -> 0 bytes. v2 raises the bar, not the fix.');
console.log('\nRead: the STRING is not the ADDRESS. Pin in undici\'s connect hook so');
console.log('validation and the socket use the same resolved IP -- no rebinding window.');
