#!/usr/bin/env bash
# Topic 1's C++ probe, both numbers, in one command.
#
#   ./measure.sh
#
# Two proxies for interface surface that no amount of arguing can talk down:
#   1. preprocessed translation-unit size -- how much text a consumer's compiler
#      has to read because of what your header dragged in
#   2. rebuild seconds after touching ONE header -- the physical dependency,
#      priced
set -euo pipefail
cd "$(dirname "$0")"
CXX="${CXX:-g++}"
STD="-std=c++20"

echo "compiler: $($CXX --version | head -1)"
echo
printf '%-10s %14s %14s\n' shape 'TU lines' 'rebuild s'
printf '%-10s %14s %14s\n' ---------- -------------- --------------

for shape in shallow deep; do
  lines=$($CXX $STD -E "$shape/api.cpp" 2>/dev/null | wc -l | tr -d ' ')

  # Warm the cache, then touch the header every consumer includes and time the
  # rebuild. Best of three: a single sample on a laptop is mostly noise.
  $CXX $STD -O2 -o "/tmp/t1_$shape" "$shape/api.cpp"
  header=$([ "$shape" = shallow ] && echo "$shape/order_types.hpp" || echo "$shape/orders.hpp")
  best=""
  for _ in 1 2 3; do
    touch "$header"
    s=$( { TIMEFORMAT=%R; time $CXX $STD -O2 -o "/tmp/t1_$shape" "$shape/api.cpp"; } 2>&1 )
    best=$(printf '%s\n%s\n' "$best" "$s" | grep -E '^[0-9]' | sort -g | head -1)
  done
  printf '%-10s %14s %14s\n' "$shape" "$lines" "$best"
done

echo
echo "both binaries, run:"
/tmp/t1_shallow
/tmp/t1_deep
echo
echo "Record BOTH numbers. Neither is 'the' interface surface -- they are two"
echo "different proxies, and topic 1 question 4 asks what neither of them sees."
