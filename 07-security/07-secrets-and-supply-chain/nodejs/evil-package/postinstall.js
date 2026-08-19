// Runs automatically at `npm install` time -- transitively, for every dep in
// the tree, before your own code executes. This benign version writes a marker
// with a timestamp; substitute "read process.env and POST it" for the real
// threat. `npm install --ignore-scripts` stops this; `npm ci` alone does NOT.
const fs = require('node:fs');
const marker = '/tmp/pwned.txt';
fs.writeFileSync(marker, `postinstall ran at ${new Date().toISOString()} as ${process.env.USER || 'unknown'}\n`);
console.log(`[evil-package] postinstall executed -> wrote ${marker}`);
