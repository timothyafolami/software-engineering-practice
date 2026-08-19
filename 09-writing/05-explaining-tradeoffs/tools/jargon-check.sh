#!/bin/sh
# Layer 9 Topic 5 -- rubric item 5, mechanised.
#
# WHAT THIS DEMONSTRATES: every term in your draft that your reader does not own
# as a unit, with the conversion beside it. The list lives in jargon-list.txt as
# "regex :: what to say instead", so the output is not "you used a bad word" but
# "here is the sentence you meant".
#
# WHAT TO LOOK FOR IN THE OUTPUT: hits in the three-sentence version are worth
# more than hits in the one-pager -- the short version is the one that gets
# forwarded, quoted, and repeated in a room you are not in. The one-pager is
# allowed exactly one mechanism sentence at the end; this script cannot tell
# which sentence that is, so read its hits rather than obeying them.
#
#   sh tools/jargon-check.sh                        # artifacts/05-tradeoff/*.md
#   sh tools/jargon-check.sh worked-example/three-sentences.md
#
# The list is only useful if it contains the words YOU reach for. Add them.

set -eu

here=$(cd "$(dirname "$0")/.." && pwd)      # 05-explaining-tradeoffs
root=$(cd "$here/.." && pwd)                # 09-writing
list=$here/jargon-list.txt

[ -f "$list" ] || { echo "missing jargon list: $list"; exit 1; }

# Patterns alone, for the per-file line count. Cleaned up on exit.
patterns=$(mktemp -t jargon-patterns)
trap 'rm -f "$patterns"' EXIT INT TERM
sed -e '/^#/d' -e '/^[[:space:]]*$/d' -e 's/ :: .*//' "$list" > "$patterns"

if [ "$#" -gt 0 ]; then
  files=$*
else
  files=$(find "$root/artifacts/05-tradeoff" -name '*.md' 2>/dev/null || true)
  if [ -z "$files" ]; then
    echo "Nothing in artifacts/05-tradeoff/ yet."
    echo "  \$EDITOR artifacts/05-tradeoff/one-pager.md artifacts/05-tradeoff/three-sentences.md"
    echo "  cp 05-explaining-tradeoffs/restatement-form.md artifacts/05-tradeoff/restatement.md"
    echo ""
    echo "Or run it on the worked example:"
    echo "  sh 05-explaining-tradeoffs/tools/jargon-check.sh \\"
    echo "     05-explaining-tradeoffs/worked-example/three-sentences.md"
    exit 0
  fi
fi

total=0
for f in $files; do
  [ -f "$f" ] || { echo "skip (not a file): $f"; continue; }
  echo "=== $f"
  hits=0
  # Read the list line by line so each hit can carry its own conversion.
  while IFS= read -r entry; do
    case "$entry" in ''|'#'*) continue ;; esac
    pattern=${entry%% :: *}
    plain=${entry#* :: }
    matches=$(grep -nEi -- "$pattern" "$f" 2>/dev/null || true)
    [ -z "$matches" ] && continue
    printf '%s\n' "$matches" | while IFS= read -r hit; do
      printf '  line %s: /%s/\n' "${hit%%:*}" "$pattern"
      printf '      say instead: %s\n' "$plain"
    done
    hits=$((hits + 1))
  done < "$list"
  # Recount outside the pipeline, since the loop above ran in a subshell.
  count=$(grep -cEif "$patterns" "$f" 2>/dev/null || true)
  echo "  ---- ${count:-0} line(s) with at least one hit."
  total=$((total + ${count:-0}))
  echo ""
done

echo "$total line(s) with jargon across $(printf '%s\n' $files | wc -l | tr -d ' ') file(s)."
echo "Rubric 5 asks for zero in the three-sentence version. Each hit is a"
echo "conversion you have not done yet: same fact, in a unit the reader owns."
