#!/bin/sh
# Layer 9 Topic 6 -- the baseline measurement, and the timed retrieval harness.
#
# WHAT THIS DEMONSTRATES: whether your repository's history answers "why is this
# line here" for someone who does not already know. Two modes:
#
#   sh archaeology.sh                       BASELINE. Crude proxies over the last
#                                           50 commits, plus the churn hot list --
#                                           a good hunting ground for candidate
#                                           lines. Run it before you change any
#                                           habits, and again in a month.
#
#   sh archaeology.sh <file> <start> <end>  RETRIEVAL. Every commit that touched
#                                           those lines, full message bodies, and
#                                           a stopwatch prompt. Pick lines at
#                                           least six months old and ideally not
#                                           written by you.
#
# WHAT TO LOOK FOR IN THE OUTPUT: the two ratios in the baseline. They are crude
# on purpose -- you are measuring your own change over a month, and a consistent
# crude proxy beats a precise one you will not re-run.
#
# Run it from inside the repository you want to measure. It reads only.

set -eu

command -v git >/dev/null 2>&1 || { echo "git not found on PATH."; exit 1; }
git rev-parse --git-dir >/dev/null 2>&1 || {
  echo "Not inside a git repository."
  echo "cd into your production service repo (or this lab's) and run it again:"
  echo "  sh <path to>/09-writing/06-commits-and-prs/tools/archaeology.sh"
  exit 1
}

n=$(git rev-list --count HEAD 2>/dev/null || echo 0)
if [ "$n" -eq 0 ]; then
  echo "Repository has no commits yet -- nothing to measure."
  exit 0
fi

if [ "$#" -ge 3 ]; then
  file=$1; start=$2; end=$3
  echo "=== RETRIEVAL: $file lines $start-$end"
  echo "Start a timer NOW. Rules: history only. No reading the surrounding code,"
  echo "no asking anyone, no opening the tracker unless the message names it."
  echo ""
  git log -L "$start,$end:$file" --format='commit %h  %ad  %s%n%b' --date=short \
    || echo "(git log -L failed -- check the path and that the range exists)"
  echo ""
  echo "Now answer, out loud, before you look at anything else:"
  echo "  1. Why does this line exist? (not what it does -- why)"
  echo "  2. What would break if you deleted it?"
  echo "  3. How many seconds did that take, and did the history answer it?"
  echo ""
  echo "Record seconds and success/failure. Three lines makes a fraction, and the"
  echo "fraction is the number the topic asks you to predict."
  exit 0
fi

echo "=== BASELINE -- $(git rev-parse --show-toplevel)"
echo "commits in history : $n"
last=$([ "$n" -lt 50 ] && echo "$n" || echo 50)
echo "measuring the last : $last"
echo ""

# Counted per COMMIT, not per line: a body spans many lines and counting lines
# would flatter a repository with one long message and forty empty ones.
count_bodies_matching() {
  git log -"$last" --format='%H' | while IFS= read -r h; do
    body=$(git log -1 --format='%b' "$h")
    if [ -n "$1" ]; then
      printf '%s' "$body" | grep -qiE "$1" && echo x
    else
      [ -z "$(printf '%s' "$body" | tr -d '[:space:]')" ] && echo x
    fi
  done | grep -c x || true
}

empty=$(count_bodies_matching "")
reasoning=$(count_bodies_matching "tried|rejected|instead of|considered|why now")
constraint=$(count_bodies_matching "constraint|invariant|must stay|idempotent|assumes")
trailers=$(count_bodies_matching "^(Fixes|Refs|Bug|Co-authored-by|Closes):")
bodies=$((last - empty))

printf "  commits with a non-empty body   : %s of %s\n" "$bodies" "$last"
printf "  commits naming a rejected path  : %s   <- aim for 3 in 10\n" "$reasoning"
printf "  commits naming a constraint     : %s\n" "$constraint"
printf "  commits with a machine trailer  : %s\n" "$trailers"
echo ""
echo "  These are crude proxies. Their value is that you can re-run them"
echo "  unchanged in a month and compare, which a careful hand-audit will not"
echo "  survive. Record today's numbers in the topic README's second table."
echo ""

echo "=== CANDIDATE LINES -- files with the most churn are the best hunting ground"
git log --since='2 years ago' --name-only --format= 2>/dev/null \
  | grep -v '^$' | sort | uniq -c | sort -rn | head -10 || true
echo ""
echo "Pick three lines that look arbitrary -- a magic number, a retry count, a"
echo "defensive if, a suppression comment -- from code at least six months old,"
echo "ideally not written by you. Your ecosystem's best hunting grounds are listed"
echo "in the commit-conventions.md next to this script's parent directory."
echo ""
echo "Then, for each:"
echo "  sh $0 <file> <startline> <endline>"
echo ""
echo "If this repo squash-merges every PR, git log -L will land on a squash commit"
echo "whose message is a PR title. That is still a valid measurement -- of your PR"
echo "descriptions rather than your commits. Say which one you measured."
