-- Runs once, on the primary's first boot only.
--
-- The SCHEMA is not created here on purpose. lab/local/lab_db.py owns it and
-- every topic program calls ensure_* itself, so the same code seeds the local
-- path and this one and there is exactly one definition of the schema in the
-- repository. Two definitions would drift, and the drift would show up as a
-- topic that works on one path and not the other.

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- A replication user for the standby. pg_basebackup needs REPLICATION, and the
-- lab user already has SUPERUSER from POSTGRES_USER, so this exists mainly to
-- make the grant visible rather than implied.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lab') THEN
        CREATE ROLE lab LOGIN REPLICATION PASSWORD 'lab';
    ELSE
        ALTER ROLE lab REPLICATION;
    END IF;
END
$$;
