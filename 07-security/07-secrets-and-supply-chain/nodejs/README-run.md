# Part B (Node) — install scripts run before your code does

```
rm -f /tmp/pwned.txt
npm install ./evil-package                 && ls -l /tmp/pwned.txt   # marker exists: postinstall ran
rm -f /tmp/pwned.txt
npm install ./evil-package --ignore-scripts; ls -l /tmp/pwned.txt 2>/dev/null || echo "no marker: scripts skipped"
```

`preinstall`/`install`/`postinstall` run for every dependency in the tree,
transitively, before you run a single line of your own code. `npm ci` against a
committed `package-lock.json` fixes VERSIONS and integrity hashes;
`--ignore-scripts` fixes EXECUTION. They are separate controls and you need
both — and `--ignore-scripts` must be set where the install actually happens
(the CI runner), not just in your terminal. Verify on the runner with
`npm config get ignore-scripts`.
