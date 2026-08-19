#!/bin/sh
# Layer 9 Topic 7 -- rubric items 1 and 6, mechanised.
#
# WHAT THIS DEMONSTRATES: every numeral in a draft, so you can answer one
# question per line -- did I measure this, or does it carry its source? A number
# that is neither is the thing this topic exists to keep out of published work,
# because a published number is read as measured whether or not it was.
#
# It also reports the CONDITIONS a reader would need to check you: machine, OS,
# architecture, versions, load shape. A number without conditions is unfalsifiable
# in exactly the way Topic 1 warns about -- nobody can show up and prove it wrong,
# which is the entire return on publishing.
#
# WHAT TO LOOK FOR IN THE OUTPUT: the CONDITIONS block first. If it is empty, the
# numbers below it cannot be checked by anyone, and the post is an assertion.
#
#   sh tools/scarcity-check.sh                          # artifacts/07-posts/*.md
#   sh tools/scarcity-check.sh path/to/draft.md
#
# This does NOT replace the sanitisation gate. Run that first, and before you
# write:  python3 lab/tools/sanitise_gate.py

set -eu

root=$(cd "$(dirname "$0")/../.." && pwd)   # 09-writing

if [ "$#" -gt 0 ]; then
  files=$*
else
  files=$(find "$root/artifacts/07-posts" -name '*.md' 2>/dev/null || true)
  if [ -z "$files" ]; then
    echo "Nothing in artifacts/07-posts/ yet."
    echo "  cp 07-writing-publicly/post-skeleton.md artifacts/07-posts/<yyyy-mm>-<slug>.md"
    echo ""
    echo "And before you write a word:"
    echo "  python3 lab/tools/sanitise_gate.py"
    exit 0
  fi
fi

for f in $files; do
  [ -f "$f" ] || { echo "skip (not a file): $f"; continue; }
  echo "=== $f"

  echo "  CONDITIONS a reader needs in order to check you:"
  found=0
  for term in "macOS|Linux|Darwin|Ubuntu|Windows" "arm64|aarch64|x86_64|amd64|M1|M2|M3" \
              "version|v[0-9]+\.[0-9]+|[0-9]+\.[0-9]+\.[0-9]+" "req/s|RPS|VUs|open.loop|closed.loop|concurrency" \
              "-O[0-3]|--release|debug build|GOMAXPROCS"; do
    hit=$(grep -nEi -- "$term" "$f" | head -2 || true)
    if [ -n "$hit" ]; then
      printf '    ok   /%s/\n' "$term"
      found=$((found + 1))
    else
      printf '    --   /%s/  not stated\n' "$term"
    fi
  done
  echo "    $found of 5 condition classes present."
  echo ""

  echo "  NUMERALS -- for each: did you measure it, or does it carry its source?"
  grep -nE "[0-9]" "$f" \
    | grep -vE "^[0-9]+:\s*(#|\[|\|-)" \
    | grep -vE "^[0-9]+:.*<[^>]*>" \
    | head -40 \
    | sed 's/^/    /' || true
  echo ""
  echo "  (any line containing a <placeholder> is filtered out -- a placeholder is a"
  echo "   debt, not a claim. Note the cost of that crudeness: a line carrying BOTH a"
  echo "   placeholder and a real numeral is hidden too, so re-read the placeholder"
  echo "   lines by hand before you publish.)"
  echo ""

  echo "  GATE REMINDERS -- neither is checkable here:"
  echo "    [ ] Employer sign-off in writing, if any of this derives from a"
  echo "        production incident."
  echo "    [ ] Sent to three engineers with: \"anything here you would push back on?\""
  echo ""
done
