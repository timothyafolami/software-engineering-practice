#!/bin/sh
# Layer 9 Topic 3 -- rubric items 1, 2, 3, 4 and 6, mechanised.
#
# WHAT THIS DEMONSTRATES: whether the RFC carries the four things that turn a
# design doc into a process (named reviewers, a decision date, a current state,
# a written decision with a dissent line), and whether every ledger row actually
# lands somewhere in the document. A ledger row with an empty "where it landed"
# column is an objection that was heard and absorbed, which is the failure this
# whole topic is about.
#
# WHAT TO LOOK FOR IN THE OUTPUT: the ledger summary. Rows with an empty last
# column, and the presence of at least one `rejected` row that still names a flip
# condition -- rubric item 3, the row worth more than the accepted ones.
#
#   sh tools/rfc-check.sh                             # artifacts/03-rfc/*
#   sh tools/rfc-check.sh rfc.md ledger.md            # named files
#
# It cannot check rubric 5 (does the superseded doc point here?) or the dissent
# line's fairness. Those are at the bottom of rubric.md.

set -eu

root=$(cd "$(dirname "$0")/../.." && pwd)

if [ "$#" -ge 1 ]; then
  rfc=$1
  ledger=${2:-}
else
  rfc=$(find "$root/artifacts/03-rfc" -name 'rfc-*.md' 2>/dev/null | head -1 || true)
  ledger=$(find "$root/artifacts/03-rfc" -name 'ledger*.md' 2>/dev/null | head -1 || true)
  if [ -z "$rfc" ] && [ -z "$ledger" ]; then
    echo "Nothing in artifacts/03-rfc/ yet. Start it:"
    echo "  cp artifacts/01-design-doc/<slug>.md artifacts/03-rfc/rfc-<slug>.md"
    echo "  cp 03-the-rfc-loop/ledger-template.md artifacts/03-rfc/ledger.md"
    echo ""
    echo "Or check the worked ledger:"
    echo "  sh 03-the-rfc-loop/tools/rfc-check.sh '' 03-the-rfc-loop/worked-example-ledger.md"
    exit 0
  fi
fi

if [ -n "$rfc" ] && [ -f "$rfc" ]; then
  echo "=== RFC: $rfc"
  head -20 "$rfc" | grep -qE '^\*\*Status:' \
    && echo "  ok    status line present: $(grep -m1 -E '^\*\*Status:' "$rfc")" \
    || { echo "  FAIL  no '**Status:' line in the first 20 lines (rubric 4)";
         echo "        paste the header block from 03-the-rfc-loop/ledger-template.md"; }
  grep -qE '^\*\*Decision:' "$rfc" \
    && echo "  ok    decision line present" \
    || echo "  note  no '**Decision:' line -- required once the state is Accepted (rubric 4)"
  grep -qE '^\*\*Dissent on record:' "$rfc" \
    && echo "  ok    dissent line present" \
    || echo "  note  no '**Dissent on record:' line (rubric 6). 'Nobody disagreed' is itself a finding -- write it"
  grep -qiE '^Reviewers:' "$rfc" \
    && echo "  ok    reviewers named" \
    || echo "  FAIL  no 'Reviewers:' line (rubric 1). A channel is not a reviewer"
  grep -qiE 'Decision needed by:' "$rfc" \
    && echo "  ok    decision date present" \
    || echo "  FAIL  no 'Decision needed by:' line (rubric 1). Comments arrive at the deadline or not at all"
  grep -qiE 'Superseded by' "$rfc" \
    && echo "  note  this doc mentions 'Superseded by' -- check the direction of the link (rubric 5)" || true
  echo ""
fi

[ -n "$ledger" ] && [ -f "$ledger" ] || { echo "No ledger file checked."; exit 0; }

echo "=== Ledger: $ledger"
awk -F'|' '
  # Markdown table rows only; skip the header and the |---|---| separator.
  /^\|/ {
    if ($0 ~ /^\|[ -]*-[ -|]*\|$/) { inrows = 1; next }
    if (!inrows) next
    n = NF - 2                      # leading and trailing empty fields
    if (n < 6) { print "  FAIL  row has " n " columns, expected 6: " substr($0, 1, 60); bad++; next }
    for (i = 2; i <= NF - 1; i++) { gsub(/^[ \t]+|[ \t]+$/, "", $i) }
    id = $2; verdict = tolower($5); reason = $6; landed = $7
    if (id == "") next              # blank template row, not filled in yet
    rows++
    if (landed == "") { printf "  FAIL  row %s: \"where it landed\" is empty -- absorbed, not resolved\n", id; bad++ }
    if (reason == "") { printf "  FAIL  row %s: no reason given\n", id; bad++ }
    if (verdict ~ /reject/ && verdict !~ /accept/) {
      rejected++
      if (tolower(reason " " landed) ~ /flips if|flips once|would have won|revisit/) withflip++
      else printf "  note  row %s is rejected but names no condition under which it would have won (rubric 3)\n", id
    }
  }
  END {
    print ""
    if (rows == 0) {
      print "  Ledger has no filled rows."
      print "  An empty ledger is not evidence the design is sound. See the note"
      print "  in the README on what would mean the experiment is broken."
      exit 0
    }
    printf "  %d row(s), %d rejected, %d of those naming a flip condition, %d problem(s).\n", rows, rejected, withflip, bad
    if (rejected == 0) print "  note  no rejected rows at all. Either nobody pushed back, or you accepted everything to be agreeable -- rubric 3 wants at least one."
  }
' "$ledger"
