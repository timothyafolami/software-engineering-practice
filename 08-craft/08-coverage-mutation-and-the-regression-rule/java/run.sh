#!/usr/bin/env bash
# Topic 8, Java arm: one command, no arguments.
#
# WHAT THIS DEMONSTRATES: step 1 compiles the target and the weak checks with
# plain javac -- no Maven, no network, no JUnit -- and shows one hand-applied
# mutant surviving the suite. Step 2 hands the same sources to PIT, which does
# in bulk what step 1 did once, or reports itself BLOCKED with the exact command
# that unblocks it.
#
# WHAT TO LOOK FOR: step 1 always runs. Step 2 is the measurement; if it says
# BLOCKED, that is a recorded result, not a failure.
set -uo pipefail
cd "$(dirname "$0")"

BUILD=build/classes
mkdir -p "$BUILD"

echo "=============================================================="
echo " 1/2  the weak suite, and one mutant applied by hand"
echo "=============================================================="
# Only the two dependency-free sources. PaginationWeakTest is deliberately NOT
# compiled here: it imports JUnit, which is Maven's job to fetch.
echo "building: javac -d $BUILD src/main/java/craft/core/Pagination.java \\"
echo "                          src/test/java/craft/weak/WeakChecks.java"
if ! javac -d "$BUILD" \
      src/main/java/craft/core/Pagination.java \
      src/test/java/craft/weak/WeakChecks.java; then
  echo "compilation failed -- fix that before measuring anything."
  exit 1
fi

java -cp "$BUILD" craft.weak.WeakChecks
demo_status=$?
if [ $demo_status -ne 0 ]; then
  echo "the demo reported a problem -- read it above; it is about the"
  echo "demonstration, not about PIT."
  exit $demo_status
fi

echo "=============================================================="
echo " 2/2  PIT over craft.core.*"
echo "=============================================================="
if command -v mvn >/dev/null 2>&1; then
  echo "running: mvn -q test org.pitest:pitest-maven:mutationCoverage"
  echo
  mvn -q test org.pitest:pitest-maven:mutationCoverage
  echo
  echo "report: target/pit-reports/index.html"
  echo "Read the line-by-line view: a covered line with a surviving mutant is"
  echo "exactly \"a test ran this and checked nothing\"."
else
  cat <<'BLOCKED'
BLOCKED: Maven is not installed, so pom.xml has never been parsed and PIT has
never run here.

  unblock:  brew install maven

Then the run this arm exists for:

  mvn test                                          # 3 weak tests, all green
  mvn org.pitest:pitest-maven:mutationCoverage      # the measurement
  open target/pit-reports/index.html

Two things to check in PIT's output BEFORE reading the score, both of which
silently produce a meaningless number:

  * the number of TESTS it found. Zero tests on a JUnit 5 project means the
    pitest-junit5-plugin dependency in pom.xml did not resolve, and PIT will
    report that cheerfully rather than failing.
  * the mutator GROUP. pom.xml pins DEFAULTS. A score from ALL is a different
    unit, in the same way a Stryker score is a different unit from mutmut's --
    which is step 4 of this topic's experiment, and the reason this arm shares
    its algorithm with the other four.

Incremental analysis (`withHistory` in pom.xml) is the JVM-only feature worth
seeing: run mutationCoverage twice without changing anything and watch the
second run do almost nothing. That is why per-PR mutation testing is practical
here and awkward in the Python, Rust and C++ arms.

Nothing above was run here, so no score for this arm is recorded anywhere in
this repository. Step 1 is a hand-applied single mutant, not a score.
BLOCKED
fi
