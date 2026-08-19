# Node.js — the format is machine-load-bearing, which is the trap

**The convention.** The npm ecosystem is where Conventional Commits took hold,
driven by automated semantic-versioning and changelog tooling: `feat:` bumps a
minor, `fix:` bumps a patch, `BREAKING CHANGE:` in the footer bumps a major.
Tools read your commit messages and cut releases from them.

**The consequence for you.** The message format is load-bearing for machines,
and that pulls all the attention to the prefix. A `fix:` with an empty body
passes every automated check, generates a changelog line, and is *worse* than
`fix bug` — because it looks compliant. The prefix is for the release notes. The
body is for the human, and nothing in the toolchain will ever ask you for it.

**The failure mode, concretely.** A changelog with two hundred entries, each a
sentence long, each accurate, and not one of them saying why. The release notes
are excellent. The archaeology is impossible.

**The second failure mode**, specific to this ecosystem: commit bodies get
squeezed because so much reasoning lives in PR discussion on the forge, and a PR
thread is a rendered conversation on a service, not an object in your
repository. Clone the repo in five years and the threads are not in it.

## The shape to write

Bad — compliant, and empty:

```
fix(pricing): increase retry count
```

Good — the prefix does its release-notes job, the body does the human's:

```
fix(pricing): retry 5x with jitter, not 2x

Why now: <incident/ticket ref> - checkout failures during the pricing service's
deploy window. Their rolling restart drops connections for <duration, measured
from: source>, and 2 retries at <backoff> did not span it.

Rejected first: raising the request timeout instead. Node's timers do not make
this cheaper - the socket is held for the whole window either way, and a slow
failure is worse for the caller than a fast one.

Constraint: this client is used by the checkout path, which is not idempotent
above this layer. Retries are only safe because the pricing call is a pure
quote lookup. Do not reuse this client for anything that writes.

Verified: <what you ran, and what you observed>.

Refs: #<issue>
```

Note what the prefix cannot carry: `fix(pricing):` tells the release tooling to
cut a patch. It does not tell a reader that the retry is only safe while the
endpoint stays read-only, and no version number will ever encode that.

## Archaeology in a Node repo

```bash
git log -L 40,48:src/pricingClient.js
git log --format='%H %s%n%b' -1 <sha>

# how much of your history is prefix-only, with nothing under it
git log -50 --format='%s%n%b' | grep -cE "^(feat|fix|chore|refactor|docs)"
git log -50 --format='%b' | grep -c .          # non-empty body lines

# lockfile and engine constraints are decisions nobody explains
git log --oneline -- package-lock.json | head
grep -nE '"engines"|"overrides"|"resolutions"' package.json
```

A pinned transitive dependency in `overrides` is the Node equivalent of a magic
number: someone pinned it for a reason, at a time, against a version. If the
commit that added it says `chore: pin lodash`, the reason is gone.
