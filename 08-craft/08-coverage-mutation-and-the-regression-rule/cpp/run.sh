#!/usr/bin/env bash
# Topic 8, C++ arm: one command, no arguments.
#
# WHAT THIS DEMONSTRATES: step 1 builds and runs the deliberately weak suite
# with a plain compiler, and shows one hand-applied mutant surviving it. Step 2
# rebuilds the same source with mull's IR pass and hands the binary to
# mull-runner -- or reports itself BLOCKED, with the exact command that unblocks
# it and the exact flags it would have used.
#
# WHAT TO LOOK FOR: step 1 always runs (compiler only, no network). Step 2 is
# the measurement; if it says BLOCKED, that is a recorded result, not a failure.
#
# The two build flags in step 2 are not decoration. `-fpass-plugin=` is what
# inserts every mutant into the IR in a single compilation -- the property this
# arm exists to show -- and `-grecord-command-line` is what lets mull-runner
# reconstruct how the binary was built. Drop either and mull reports nothing
# and tells you almost nothing about why.
set -uo pipefail
cd "$(dirname "$0")"

CXX=${CXX:-clang++}
STD=-std=c++23
BUILD=build
mkdir -p "$BUILD"

echo "=============================================================="
echo " 1/2  the weak suite, and one mutant applied by hand"
echo "=============================================================="
echo "building: $CXX $STD -O0 -g -Wall -Wextra -o $BUILD/weak_test pagination.cpp weak_test.cpp"
if ! "$CXX" $STD -O0 -g -Wall -Wextra -o "$BUILD/weak_test" pagination.cpp weak_test.cpp; then
  echo "compilation failed -- fix that before measuring anything."
  exit 1
fi

"$BUILD/weak_test"
suite_status=$?
echo "weak_test exit status: $suite_status   (mull reads exactly this: 0 = mutant survived)"
echo
if [ $suite_status -ne 0 ]; then
  echo "the weak suite did not pass -- fix that before measuring anything."
  exit $suite_status
fi

echo "=============================================================="
echo " 2/2  mull over pagination.cpp"
echo "=============================================================="

# mull ships one plugin and one runner per LLVM major version, and they must
# match each other AND the clang doing the compiling. Auto-detect, but let the
# environment win: MULL_PLUGIN and MULL_RUNNER override.
plugin=${MULL_PLUGIN:-}
runner=${MULL_RUNNER:-}
if [ -z "$plugin" ]; then
  for prefix in /opt/homebrew /usr/local /usr; do
    for candidate in "$prefix"/lib/mull-ir-frontend-*; do
      [ -e "$candidate" ] && plugin="$candidate" && break 2
    done
  done
fi
if [ -z "$runner" ]; then
  for candidate in mull-runner mull-runner-19 mull-runner-18 mull-runner-17; do
    command -v "$candidate" >/dev/null 2>&1 && runner="$candidate" && break
  done
fi

if [ -n "$plugin" ] && [ -n "$runner" ]; then
  echo "plugin: $plugin"
  echo "runner: $runner"
  echo
  echo "building: $CXX $STD -O0 -g -grecord-command-line -fpass-plugin=$plugin \\"
  echo "            -o $BUILD/weak_test_mull pagination.cpp weak_test.cpp"
  "$CXX" $STD -O0 -g -grecord-command-line -fpass-plugin="$plugin" \
    -o "$BUILD/weak_test_mull" pagination.cpp weak_test.cpp || exit 1
  echo
  echo "running: $runner --report-name topic8 --reporters IDE --reporters SQLite $BUILD/weak_test_mull"
  echo
  "$runner" --report-name topic8 --reporters IDE --reporters SQLite "$BUILD/weak_test_mull"
else
  cat <<'BLOCKED'
BLOCKED: mull is not installed (no mull-ir-frontend-* plugin and no mull-runner
on PATH).

  unblock:  brew tap mull-project/mull && brew install mull

mull is pinned to one LLVM major version: the plugin, the runner and the clang
that compiles must all agree, so on Apple Silicon expect to install Homebrew's
matching llvm alongside it and to compile with THAT clang++ rather than Apple's
(`CXX=$(brew --prefix llvm)/bin/clang++ ./run.sh`). Check the tap's own README
for the version pairing before assuming Apple clang will do.

Then the run this arm exists for, unchanged:

  clang++ -std=c++23 -O0 -g -grecord-command-line \
    -fpass-plugin=$(brew --prefix)/lib/mull-ir-frontend-<LLVM_MAJOR> \
    -o build/weak_test_mull pagination.cpp weak_test.cpp

  mull-runner --report-name topic8 --reporters IDE --reporters SQLite \
    build/weak_test_mull

  -fpass-plugin        inserts EVERY mutant into the IR in ONE compilation.
                       This is the property this arm exists to demonstrate: no
                       recompile per mutant, unlike cargo-mutants next door.
  -g                   mull reports mutants by source location; without debug
                       info you get addresses.
  -grecord-command-line  mull-runner reads the recorded command line back out
                       of the binary. Omit it and the run is far less useful.
  -O0                  optimisation can delete a mutated expression outright,
                       which turns a real mutant into a phantom survivor.

Nothing above was run here, so no score for this arm is recorded anywhere in
this repository. Step 1 is a hand-applied single mutant, not a score.

Read the survivors from the IDE reporter's output, and remember the hazard in
pagination.hpp's header: a C++ mutant can produce undefined behaviour, and
mull reads any non-zero exit as a kill -- it cannot tell a failed assertion
from a segfault. Decide what you record before you run it.
BLOCKED
fi
