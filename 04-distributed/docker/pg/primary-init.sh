#!/bin/bash
# Runs once, inside /docker-entrypoint-initdb.d, with the server already up.
# The official image's generated pg_hba.conf allows replication only from
# 127.0.0.1 -- a standby in another container is not that, and pg_basebackup
# fails with "no pg_hba.conf entry for replication connection". This adds it.
set -e
echo "host replication all all trust" >> "$PGDATA/pg_hba.conf"
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -c "SELECT pg_reload_conf();"
