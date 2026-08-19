// Layer 7 · Topic 3 — XSS by output context (Node / React).
//
// `npm install && node xss_context.js`. React + react-dom are not in this
// machine's offline cache, so this file is idiomatic-but-blocked until that
// one-time online install; it uses React.createElement (not JSX) so no build
// step is required once the packages exist.
//
// React's contribution (README): JSX auto-escapes TEXT children structurally
// -- you cannot "forget" it for a text child -- while its holes are
// enumerable: dangerouslySetInnerHTML, href={userValue} with a javascript:
// URL, and ref-based direct DOM writes. Its limit is identical to Jinja's: it
// does not model attribute or URL contexts. This program renders the same
// payload four ways with renderToStaticMarkup and prints the emitted markup
// plus whether it would execute in a browser.
//
// What to look for: the text child is neutralized (structural escape), but
// dangerouslySetInnerHTML and the javascript: href both EXECUTE -- the same
// URL-context miss Jinja has and Go's html/template does not.

const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

const h = React.createElement;

function executes(context, html) {
  const low = html.toLowerCase();
  if (context === 'text' || context === 'dangerous')
    return low.includes('<script>beacon()</script>'); // a live script element
  if (context === 'href')
    return low.includes('href="javascript:beacon()"'); // live javascript: scheme
  if (context === 'attr')
    return /\son\w+="?beacon\(\)/.test(low);           // a real event-handler attribute
  return false;
}

const PAYLOAD_SCRIPT = '<script>beacon()</script>';
const CELLS = [
  ['text', 'text child (safe)',
    () => h('div', null, PAYLOAD_SCRIPT)],
  ['dangerous', 'dangerouslySetInnerHTML',
    () => h('div', { dangerouslySetInnerHTML: { __html: PAYLOAD_SCRIPT } })],
  ['href', 'javascript: URL',
    () => h('a', { href: 'javascript:beacon()' }, 'x')],
  ['attr', 'attribute break-out attempt',
    () => h('a', { title: '" onmouseover=beacon() x="' }, 'x')],
];

console.log('Layer 7 · Topic 3 — XSS by output context (Node / React)\n');
console.log(`  ${'context'.padEnd(28)}${'executes?'.padEnd(10)}emitted markup`);
let executed = 0;
for (const [ctx, name, build] of CELLS) {
  const html = renderToStaticMarkup(build());
  const ex = executes(ctx, html);
  executed += ex ? 1 : 0;
  console.log(`  ${name.padEnd(28)}${(ex ? 'YES' : 'NO').padEnd(10)}${html}`);
}
console.log(`  -> ${executed}/4 payloads executed\n`);
console.log('Read: JSX escapes the text child so you cannot forget it, and it ' +
  'escapes the title attribute value. But dangerouslySetInnerHTML injects raw ' +
  'HTML by name, and href={"javascript:..."} is not URL-sanitized (React only ' +
  'warns) -- both execute. React, like Jinja, does not model URL context; Go ' +
  'html/template does. That is the whole axis this topic measures.');
