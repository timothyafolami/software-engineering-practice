#!/usr/bin/env bash
# Topic 8, experiment 5: mechanise "every bug fix gets a test that fails before the fix".
#
# WHAT THIS DEMONSTRATES: a regression test written AFTER the fix, against the
# fixed code, has never demonstrated that it can detect the bug -- and a
# distressing fraction of them cannot. This script checks out the PARENT
# commit's application source, keeps the NEW test, runs it, and FAILS THE BUILD
# IF THE TEST PASSES.
#
# WHAT TO LOOK FOR: it reverts `api/app/` ONLY. Reverting the tests along with
# the source trivially makes everything pass and makes the gate worse than
# nothing -- that is the single most common bug in scripts of this shape and it
# is topic 8's fourth broken-experiment note.
#
#   make regression BUG=pagination-ties
#
# Wire it in CI on any PR labelled `bugfix`.
set -euo pipefail

BUG="${1:?usage: regression.sh <bug-slug>}"
LAB="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API="$LAB/api"
TEST="$API/tests/regression/test_${BUG//-/_}.py"

[ -f "$TEST" ] || { echo "no regression test at $TEST" >&2; exit 2; }
git -C "$LAB" rev-parse --git-dir >/dev/null 2>&1 || {
  echo "not a git repository: $LAB" >&2
  echo "  git init && git add -A && git commit -m 'lab baseline'" >&2
  exit 2
}

PARENT="$(git -C "$LAB" rev-parse HEAD^)"
STASH="$(mktemp -d)"
echo "== regression gate: $BUG"
echo "   test   : ${TEST#"$LAB"/}"
echo "   parent : $PARENT"

cleanup() {
  # Restore the working tree no matter how we exit -- a gate that can leave the
  # checkout reverted is a gate nobody will run twice.
  git -C "$LAB" checkout -- api/app 2>/dev/null || true
  rm -rf "$STASH"
}
trap cleanup EXIT

# Revert the SOURCE only. The test stays exactly as the PR wrote it.
git -C "$LAB" checkout "$PARENT" -- api/app

set +e
( cd "$API" && DATABASE_URL="sqlite+aiosqlite:///:memory:" python3 -m pytest "$TEST" -q )
STATUS=$?
set -e

if [ "$STATUS" -eq 0 ]; then
  cat >&2 <<MSG

FAIL: the regression test PASSED against the pre-fix source.

That means it cannot detect the bug it was written for. Either it asserts
something the bug never broke, or it exercises a path the fix did not change.
Rewrite it, watch it fail, then commit.
MSG
  exit 1
fi

echo
echo "OK: the regression test fails against the pre-fix source and passes against HEAD."
echo "    It has now been watched to fail, which is the only thing that makes it a regression test."
