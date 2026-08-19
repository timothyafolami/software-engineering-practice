# Layer 8 lab targets. Every command in a topic README's "How to run" that is
# longer than one line lives here instead, so the README and the build agree.
#
#   make seed         SEED_ORDERS orders across ~2000 customers, tie-heavy
#   make coverage     statement vs branch, the topic 8 comparison
#   make mutation     mutmut over the two pure modules
#   make snapshot     regenerate openapi.snapshot.json
#   make check-schema CI gate: fail if the live schema drifted from the snapshot
#   make regression BUG=<slug>   topic 8's mechanised regression rule

PY ?= python3
SEED_ORDERS ?= 50000
DATABASE_URL ?= postgresql+asyncpg://app:app@toxiproxy:5433/craft_lab

.PHONY: seed coverage coverage-sysmon mutation snapshot check-schema regression unit properties

seed:
	SEED_ORDERS=$(SEED_ORDERS) DATABASE_URL=$(DATABASE_URL) $(PY) seed.py

unit:
	DATABASE_URL=sqlite+aiosqlite:///:memory: $(PY) -m pytest tests/unit -q

properties:
	DATABASE_URL=sqlite+aiosqlite:///:memory: $(PY) -m pytest tests/properties -q

coverage:
	DATABASE_URL=sqlite+aiosqlite:///:memory: $(PY) -m coverage run --branch -m pytest tests/unit -q
	$(PY) -m coverage report -m

# 3.12+: sysmon is substantially faster than the old trace function. Compare the
# wall clock against the target above. Plugins and dynamic contexts are not
# supported under sysmon -- check that before flipping it in CI.
coverage-sysmon:
	COVERAGE_CORE=sysmon DATABASE_URL=sqlite+aiosqlite:///:memory: \
	  $(PY) -m coverage run --branch -m pytest tests/unit -q
	$(PY) -m coverage report -m

mutation:
	DATABASE_URL=sqlite+aiosqlite:///:memory: mutmut run \
	  --paths-to-mutate app/core/pagination.py,app/core/money.py
	mutmut results

snapshot:
	$(PY) snapshot_openapi.py

check-schema:
	$(PY) snapshot_openapi.py --check

# Topic 8, experiment 5. Checks out the PARENT commit's app/ source, keeps the
# NEW test, runs it, and fails if the test passes -- because a regression test
# that has never been watched to fail has never demonstrated it can detect
# anything. See tools/regression.sh for why it reverts app/ only.
regression:
	@test -n "$(BUG)" || (echo "usage: make regression BUG=<slug>"; exit 2)
	../tools/regression.sh $(BUG)
