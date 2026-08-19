#!/bin/bash
# Runs once, on the primary's first boot, BEFORE 01_extensions.sql.
#
# WHY THIS FILE EXISTS: the postgres image's entrypoint appends exactly one
# blanket rule to pg_hba.conf --
#
#     host all all all scram-sha-256
#
# -- and `all` there means all DATABASES, which does not include the special
# `replication` pseudo-database. The only replication rules initdb writes are
# for 127.0.0.1 and ::1. A standby in another container is neither, so
# `pg_basebackup -h postgres-primary` fails at the door with:
#
#     FATAL: no pg_hba.conf entry for replication connection from host
#     "172.x.x.x", user "lab", no encryption
#
# and the postgres-replica service exits before it has a data directory. Topic 8
# cannot start on the Docker path without this line.
#
# Scoped to the replication role rather than to `all`, and left on scram-sha-256
# rather than trust, so this is the rule you would actually write in production.
set -e

cat >> "$PGDATA/pg_hba.conf" <<EOF

# Added by lab/docker/postgres/init/00_replication_hba.sh -- lets the
# postgres-replica service run pg_basebackup across the compose network.
host    replication     ${POSTGRES_USER}     all     scram-sha-256
EOF

echo "[init] pg_hba.conf: replication allowed for ${POSTGRES_USER} from the compose network"
