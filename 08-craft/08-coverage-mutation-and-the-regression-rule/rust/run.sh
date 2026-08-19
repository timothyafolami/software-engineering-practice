#!/usr/bin/env bash
# Topic 8, Rust arm: one command, no arguments.
#
# WHAT THIS DEMONSTRATES: step 1 runs the deliberately weak suite and shows one
# hand-applied mutant surviving it. Step 2 hands the same crate to cargo-mutants,
# which does in bulk what step 1 did once -- or reports itself BLOCKED, with the
# exact command that unblocks it.
#
# WHAT TO LOOK FOR: step 1 always runs (std library only, no network). Step 2 is
# the measurement; if it says BLOCKED, that is a recorded result, not a failure.
set -uo pipefail
cd "$(dirname "$0")"

echo "=============================================================="
echo " 1/2  the weak suite, and one mutant applied by hand"
echo "=============================================================="
cargo test --quiet -- --nocapture --test-threads=1
suite_status=$?
echo
if [ $suite_status -ne 0 ]; then
  echo "the weak suite did not pass -- fix that before measuring anything."
  exit $suite_status
fi

echo "=============================================================="
echo " 2/2  cargo-mutants over src/lib.rs"
echo "=============================================================="
if cargo mutants --version >/dev/null 2>&1; then
  echo "running: cargo mutants --file src/lib.rs --caught --unviable"
  echo
  cargo mutants --file src/lib.rs --caught --unviable
  echo
  echo "results also written to mutants.out/ :"
  echo "  missed.txt    the survivors -- for each one, name the missing ASSERTION"
  echo "  unviable.txt  mutants the compiler rejected; this is the count that"
  echo "                distinguishes this arm from the Python one"
  echo "  outcomes.json the machine-readable version, for your table"
else
  cat <<'BLOCKED'
BLOCKED: cargo-mutants is not installed.

  unblock:  cargo install --locked cargo-mutants

Then the run this arm exists for, unchanged:

  cargo mutants --file src/lib.rs --caught --unviable

and the two flags that matter when you read the output:

  --caught     also list the mutants the suite killed, not only the survivors
  --unviable   also list the mutants that failed to COMPILE -- the category
               Python's source-level tooling cannot separate out, and the
               reason a cargo-mutants denominator and a mutmut denominator are
               not the same unit
  --timeout-multiplier 5   already set in mutants.toml; raise it before
               concluding that a survivor is a timeout

Nothing above was run here, so no score for this arm is recorded anywhere in
this repository. Step 1 above is a hand-applied single mutant, not a score.
BLOCKED
fi
