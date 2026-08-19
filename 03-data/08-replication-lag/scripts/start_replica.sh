#!/usr/bin/env bash
# Bring up a streaming replica of the local Postgres on port 5433.
#
#   bash 08-replication-lag/scripts/start_replica.sh          # apply delay 2s
#   APPLY_DELAY=0 bash 08-replication-lag/scripts/start_replica.sh
#   bash 08-replication-lag/scripts/start_replica.sh --stop
#
# WHY recovery_min_apply_delay = '2s' BY DEFAULT: it makes the lag
# DETERMINISTIC. A bug you can reproduce on demand is a different object from
# one you can only observe, and every experiment in this topic depends on being
# able to produce a stale read whenever you want one. Set APPLY_DELAY=0 to get
# natural lag instead, and notice how much harder the same bug becomes to see --
# that comparison is the staging-versus-production lesson in one measurement.
#
# THIS STARTS A DAEMON. It is a script rather than something a Python program
# does for you on purpose: bringing up a second database cluster is a decision,
# not a side effect. Nothing else in this layer starts a server.
#
# WHAT IT DOES, in order:
#   1. checks the primary is up and has WAL senders available
#   2. pg_basebackup -R into REPLICA_DIR (a full physical copy, so this costs
#      as much disk as your primary currently uses)
#   3. writes port + recovery_min_apply_delay into the standby's config
#   4. pg_ctl start, then waits for pg_is_in_recovery() to report true
#
# Everything it creates lives under REPLICA_DIR and the --stop flag removes it.

set -euo pipefail

# macOS, and only macOS: without an explicit locale in the environment the
# postmaster can spawn a thread while resolving one during startup and then
# refuse to run --
#     FATAL: postmaster became multithreaded during startup
#     HINT:  Set the LC_ALL environment variable to a valid locale.
# The primary you already have running was started by brew services, which sets
# one; a standby started from an interactive shell inherits whatever you have,
# which on this machine is nothing. Setting it here costs nothing on Linux.
export LC_ALL="${LC_ALL:-C}"

PRIMARY_PORT="${PRIMARY_PORT:-5432}"
PRIMARY_HOST="${PRIMARY_HOST:-/tmp}"
REPLICA_PORT="${REPLICA_PORT:-5433}"
REPLICA_DIR="${REPLICA_DIR:-${TMPDIR:-/tmp}/sep_lab_03_replica}"
APPLY_DELAY="${APPLY_DELAY:-2s}"
SLOT_NAME="${SLOT_NAME:-sep_lab_standby}"

log() { printf '[replica] %s\n' "$*"; }

stop_replica() {
  if [ -d "$REPLICA_DIR" ]; then
    log "stopping and removing $REPLICA_DIR"
    pg_ctl -D "$REPLICA_DIR" -m fast stop >/dev/null 2>&1 || true
    rm -rf "$REPLICA_DIR"
  fi
  psql -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -d postgres -tAc \
    "SELECT pg_drop_replication_slot('$SLOT_NAME') WHERE EXISTS
     (SELECT 1 FROM pg_replication_slots WHERE slot_name = '$SLOT_NAME')" >/dev/null 2>&1 || true
  log "stopped. The primary is untouched."
}

if [ "${1:-}" = "--stop" ]; then
  stop_replica
  exit 0
fi

for tool in pg_basebackup pg_ctl psql pg_isready; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "missing $tool -- install the Postgres client package" >&2
    exit 1
  }
done

pg_isready -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" >/dev/null || {
  echo "no primary on $PRIMARY_HOST:$PRIMARY_PORT" >&2
  echo "unblock: brew services start postgresql@17" >&2
  exit 1
}

# A standby needs a WAL sender slot on the primary and wal_level >= replica.
# Both are the default; checking is cheap and the failure is otherwise obscure.
wal_level=$(psql -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -d postgres -tAc "SHOW wal_level")
senders=$(psql -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -d postgres -tAc "SHOW max_wal_senders")
log "primary wal_level=$wal_level max_wal_senders=$senders"
if [ "$wal_level" = "minimal" ] || [ "$senders" = "0" ]; then
  cat >&2 <<MSG
BLOCKED: this primary cannot serve a standby.
unblock:
  psql -d postgres -c "ALTER SYSTEM SET wal_level = 'replica';"
  psql -d postgres -c "ALTER SYSTEM SET max_wal_senders = 10;"
  brew services restart postgresql@17
MSG
  exit 1
fi

if [ -d "$REPLICA_DIR" ]; then
  log "$REPLICA_DIR already exists -- removing it first"
  stop_replica
fi

log "creating replication slot $SLOT_NAME on the primary"
psql -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -d postgres -tAc \
  "SELECT pg_create_physical_replication_slot('$SLOT_NAME')" >/dev/null

log "pg_basebackup -> $REPLICA_DIR (this copies the whole cluster)"
pg_basebackup \
  -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" \
  -D "$REPLICA_DIR" \
  -S "$SLOT_NAME" \
  -R -P -X stream

cat >> "$REPLICA_DIR/postgresql.auto.conf" <<CONF

# --- added by 08-replication-lag/scripts/start_replica.sh ---
port = $REPLICA_PORT
hot_standby = on
# The knob this whole topic turns on: replay is held back by this much, so lag
# is a number you chose rather than a number you waited for.
recovery_min_apply_delay = '$APPLY_DELAY'
CONF

log "starting standby on port $REPLICA_PORT with recovery_min_apply_delay=$APPLY_DELAY"
pg_ctl -D "$REPLICA_DIR" -l "$REPLICA_DIR/standby.log" start

for _ in $(seq 1 30); do
  if psql -h "$PRIMARY_HOST" -p "$REPLICA_PORT" -d postgres -tAc \
      "SELECT pg_is_in_recovery()" 2>/dev/null | grep -q t; then
    log "standby is up and in recovery"
    log ""
    log "export LAB_REPLICA_DSN=\"postgresql://?host=$PRIMARY_HOST&port=$REPLICA_PORT&dbname=sep_lab_03_data\""
    log "then: python3 08-replication-lag/scripts/wait_for_replica.py"
    log "stop it later with: bash 08-replication-lag/scripts/start_replica.sh --stop"
    exit 0
  fi
  sleep 1
done

echo "standby did not report pg_is_in_recovery() within 30s" >&2
echo "see $REPLICA_DIR/standby.log" >&2
exit 1
