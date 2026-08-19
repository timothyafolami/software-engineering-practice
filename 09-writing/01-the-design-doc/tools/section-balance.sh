#!/bin/sh
# Layer 9 Topic 1 -- rubric item 7, measured instead of eyeballed.
#
# WHAT THIS DEMONSTRATES: the word balance of a design doc. If "Proposed design"
# outweighs "Context" + "Alternatives considered" by more than 2:1, you have
# written a plan, not a design doc -- the reader has your conclusion and not the
# constraints they would need to disagree with it.
#
# WHAT TO LOOK FOR IN THE OUTPUT: the ratio line. Also look at Context on its own:
# a Context section under ~150 words is usually a doc that assumes the reader has
# spent your last three weeks.
#
#   sh tools/section-balance.sh                 # every draft in artifacts/01-design-doc
#   sh tools/section-balance.sh path/to/doc.md  # one file
#
# POSIX sh + awk only: no GNU flags, works with the macOS awk that ships here.

set -eu

root=$(cd "$(dirname "$0")/../.." && pwd)   # the 09-writing directory

if [ "$#" -gt 0 ]; then
  files=$*
else
  files=$(find "$root/artifacts/01-design-doc" -name '*.md' 2>/dev/null || true)
  if [ -z "$files" ]; then
    echo "No drafts found in artifacts/01-design-doc/."
    echo "Start one:  cp templates/design-doc.md artifacts/01-design-doc/<slug>.md"
    echo "Or measure the worked example (paths below are from the 09-writing root):"
    echo "  sh 01-the-design-doc/tools/section-balance.sh 01-the-design-doc/worked-example.md"
    exit 0
  fi
fi

for f in $files; do
  [ -f "$f" ] || { echo "skip (not a file): $f"; continue; }
  echo "=== $f"
  awk '
    /^## / {
      section = substr($0, 4)
      order[++n] = section
      next
    }
    section != "" { words[section] += NF }
    END {
      printf "  %-26s %s\n", "SECTION", "WORDS"
      for (i = 1; i <= n; i++) {
        s = order[i]
        printf "  %-26.26s %5d\n", s, words[s]
      }
      proposal = words["Proposed design"]
      thinking = words["Context"] + words["Alternatives considered"]
      print ""
      printf "  Context + Alternatives : %d\n", thinking
      printf "  Proposed design        : %d\n", proposal
      if (thinking == 0) {
        print "  RATIO                  : n/a -- no Context or Alternatives section found."
        print "  (Section headings must match templates/design-doc.md exactly.)"
      } else {
        r = proposal / thinking
        printf "  RATIO (proposal:think) : %.2f:1", r
        if (r > 2) print "   <- rubric 7 FAILS: this is a plan, not a design doc"
        else print "   <- within rubric 7"
      }
    }
  ' "$f"
  echo ""
done
