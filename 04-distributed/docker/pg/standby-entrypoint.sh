#!/bin/bash
# pg-standby: a real streaming standby of pg-primary, with
# recovery_min_apply_delay driven by APPLY_DELAY (../lab/README.md).
#
# PGDATA IS NOT /var/lib/postgresql/data ON postgres:18. The image moved it to
# /var/lib/postgresql/18/docker and declares /var/lib/postgresql as the volume.
# Hardcoding the 16/17 path gets you a root-owned directory and
#   pg_basebackup: error: could not create directory ".../pg_wal": Permission denied
# Take PGDATA from the image's own environment.
set -e
: "${APPLY_DELAY:=0}"
: "${PGDATA:=/var/lib/postgresql/18/docker}"

until pg_isready -h pg-primary -U postgres -q; do
  echo "standby: waiting for pg-primary"; sleep 1
done

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "standby: base backup from pg-primary into $PGDATA"
  mkdir -p "$PGDATA"
  rm -rf "${PGDATA:?}"/*
  pg_basebackup -h pg-primary -U postgres -D "$PGDATA" -Fp -Xs -R -w
fi

# -R already wrote primary_conninfo + standby.signal. Rewrite the delay on every
# boot so APPLY_DELAY is honoured without needing a fresh volume.
sed -i '/recovery_min_apply_delay/d' "$PGDATA/postgresql.auto.conf"
echo "recovery_min_apply_delay = '$APPLY_DELAY'" >> "$PGDATA/postgresql.auto.conf"
chmod 0700 "$PGDATA"
echo "standby: recovery_min_apply_delay = '$APPLY_DELAY'"
exec postgres -D "$PGDATA" -c hot_standby=on
