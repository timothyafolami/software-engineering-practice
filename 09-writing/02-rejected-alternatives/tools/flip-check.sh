#!/bin/sh
# Layer 9 Topic 2 -- rubric items 1, 2 and 3, mechanised.
#
# WHAT THIS DEMONSTRATES: for each "**Alternative:" block in an alternatives
# section, whether the rejection (a) names a condition that would flip it, and
# (b) contains at least one fact about YOUR system -- a number, a config value, a
# placeholder you still owe. A rejection with neither is a category judgement
# about a technology, and you could have written it without reading the problem.
# It also checks that "do nothing" is present at all, because it is the
# alternative most likely to be right and least likely to be written.
#
# WHAT TO LOOK FOR IN THE OUTPUT: run it on version-a.md and version-b.md of the
# worked example. Version A fails every block. The difference between the two
# reports is the topic.
#
#   sh tools/flip-check.sh                        # every file in artifacts/02-alternatives
#   sh tools/flip-check.sh path/to/version-b.md   # named files
#
# This is a grep with opinions. It cannot tell whether a flip condition is
# *reachable* -- rubric item 6 (could a reviewer argue it is already true?) is
# yours.

set -eu

root=$(cd "$(dirname "$0")/../.." && pwd)   # the 09-writing directory

if [ "$#" -gt 0 ]; then
  files=$*
else
  files=$(find "$root/artifacts/02-alternatives" -name '*.md' 2>/dev/null || true)
  if [ -z "$files" ]; then
    echo "No files in artifacts/02-alternatives/."
    echo "Try the worked example, both versions, and compare the two reports:"
    echo "  sh 02-rejected-alternatives/tools/flip-check.sh \\"
    echo "     02-rejected-alternatives/worked-example/version-a.md \\"
    echo "     02-rejected-alternatives/worked-example/version-b.md"
    exit 0
  fi
fi

for f in $files; do
  [ -f "$f" ] || { echo "skip (not a file): $f"; continue; }
  echo "=== $f"
  awk '
    function flush(   flip, specific, status) {
      if (title == "") return
      flip     = (buf ~ /flips if|flips once|Revisit|revisit when|until [A-Za-z<]/)
      specific = (buf ~ /<[^>]+>|`[^`]+`|[0-9]/)
      status = (flip && specific) ? "ok  " : "FAIL"
      printf "  %s  %s\n", status, substr(title, 1, 68)
      if (!flip)     print "        - no flip condition: nothing here could be observed to become true"
      if (!specific) print "        - no fact from your system: no number, no config value, no placeholder"
      nblocks++
      if (!(flip && specific)) nbad++
      if (tolower(title) ~ /do nothing|status quo|ship nothing/) donothing = 1
      buf = ""; title = ""
    }
    /^\*\*Alternative/ {
      flush()
      title = $0
      gsub(/\*\*/, "", title)
      buf = $0
      next
    }
    { if (title != "") buf = buf " " $0 }
    END {
      flush()
      print ""
      if (nblocks == 0) {
        print "  No \"**Alternative:\" blocks found. This check keys on that prefix;"
        print "  see 02-rejected-alternatives/worked-example/version-b.md for the shape."
        exit 0
      }
      printf "  %d alternative(s), %d failing.\n", nblocks, nbad
      if (!donothing) print "  MISSING: \"do nothing\" -- rubric item 3. It is mandatory and it is the hard one."
    }
  ' "$f"
  echo ""
done

echo "Rubric item 4, by hand: read these lines with no surrounding context."
echo "Any that read like general technology advice go back."
for f in $files; do
  [ -f "$f" ] || continue
  grep -nE "Rejected|flips if|flips once|Revisit" "$f" | sed "s|^|  $(basename "$f"):|" || true
done
