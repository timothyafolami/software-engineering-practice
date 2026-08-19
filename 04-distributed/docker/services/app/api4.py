"""Topic 4 -- the `api` service. Writes to pg-primary, reads from pg-standby.

FIX=none    always read the standby
FIX=sticky  read the primary for STICKY_MS after this session's last write
FIX=lsn     poll the standby until pg_last_wal_replay_lsn() >= the session's
            write LSN, then read it; fall back to the primary on timeout

Every read records one rw_probe row, including pg_is_in_recovery() captured
INSIDE the read path. The topic README is explicit that omitting that is the
most likely way to "prove" a bug which exists does not.
"""
import os, time, uuid
from fastapi import FastAPI, Header, Query
from .db import pool

FIX = os.environ.get("FIX", "none")
APPLY_DELAY = os.environ.get("APPLY_DELAY", "unset")
RUN_ID = os.environ.get("RUN_ID", "run")
STICKY_MS = int(os.environ.get("STICKY_MS", "1000"))
LSN_TIMEOUT_MS = int(os.environ.get("LSN_TIMEOUT_MS", "2000"))
LSN_POLL_MS = int(os.environ.get("LSN_POLL_MS", "20"))

PRIMARY = os.environ["PRIMARY_DSN"]
STANDBY = os.environ["STANDBY_DSN"]

app = FastAPI()
_sessions: dict[str, tuple[str, float]] = {}   # session -> (lsn, last_write_monotonic)

DDL = """
CREATE TABLE IF NOT EXISTS entity (
    id text PRIMARY KEY,
    val text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS rw_probe (
    id bigserial PRIMARY KEY,
    run_id text NOT NULL,
    fix text NOT NULL CHECK (fix IN ('none','sticky','lsn')),
    apply_delay text NOT NULL,
    gap_ms integer NOT NULL,
    entity_id text NOT NULL,
    written_value text NOT NULL,
    read_value text,
    write_lsn pg_lsn NOT NULL,
    read_replay_lsn pg_lsn,
    read_replay_ts timestamptz,
    read_in_recovery boolean NOT NULL,
    poll_iterations integer NOT NULL DEFAULT 0,
    fell_back boolean NOT NULL DEFAULT false,
    read_ms numeric NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS rw_probe_run_idx ON rw_probe (run_id, fix);
"""


@app.on_event("startup")
def _startup() -> None:
    with pool("PRIMARY_DSN").connection() as c:
        c.execute(DDL)


@app.get("/health")
def health():
    with pool("STANDBY_DSN").connection() as c:
        rec = c.execute("SELECT pg_is_in_recovery()").fetchone()[0]
    return {"fix": FIX, "apply_delay": APPLY_DELAY, "standby_in_recovery": rec,
            "run_id": RUN_ID}


@app.post("/write")
def write(entity: str = Query(...), value: str = Query(...),
          x_session: str = Header(default="anon")):
    with pool("PRIMARY_DSN").connection() as c:
        c.execute("INSERT INTO entity(id,val) VALUES(%s,%s) "
                  "ON CONFLICT (id) DO UPDATE SET val=EXCLUDED.val, "
                  "updated_at=clock_timestamp()", (entity, value))
        lsn = c.execute("SELECT pg_current_wal_flush_lsn()::text").fetchone()[0]
    _sessions[x_session] = (lsn, time.monotonic())
    return {"lsn": lsn}


@app.get("/read")
def read(entity: str = Query(...), expect: str = Query(...),
         gap_ms: int = Query(0), x_session: str = Header(default="anon")):
    wl, wts = _sessions.get(x_session, (None, 0.0))
    polls, fell_back = 0, False
    t0 = time.perf_counter()

    use_primary = False
    if FIX == "sticky" and wl is not None and (time.monotonic() - wts) * 1000 < STICKY_MS:
        use_primary = True
    elif FIX == "lsn" and wl is not None:
        deadline = time.monotonic() + LSN_TIMEOUT_MS / 1000
        with pool("STANDBY_DSN").connection() as c:
            while True:
                polls += 1
                r = c.execute("SELECT pg_last_wal_replay_lsn() >= %s::pg_lsn", (wl,)).fetchone()[0]
                if r:
                    break
                if time.monotonic() >= deadline:
                    fell_back, use_primary = True, True
                    break
                time.sleep(LSN_POLL_MS / 1000)

    dsn_env = "PRIMARY_DSN" if use_primary else "STANDBY_DSN"
    with pool(dsn_env).connection() as c:
        row = c.execute(
            "SELECT (SELECT val FROM entity WHERE id=%s), "
            "       pg_is_in_recovery(), "
            "       CASE WHEN pg_is_in_recovery() "
            "            THEN pg_last_wal_replay_lsn()::text ELSE NULL END, "
            "       CASE WHEN pg_is_in_recovery() "
            "            THEN pg_last_xact_replay_timestamp() ELSE NULL END, "
            "       clock_timestamp()",
            (entity,)).fetchone()
    val, in_recovery, replay, replay_ts, read_now = row
    read_ms = (time.perf_counter() - t0) * 1000

    with pool("PRIMARY_DSN").connection() as c:
        c.execute(
            "INSERT INTO rw_probe(run_id,fix,apply_delay,gap_ms,entity_id,"
            "written_value,read_value,write_lsn,read_replay_lsn,read_replay_ts,"
            "read_in_recovery,poll_iterations,fell_back,read_ms,observed_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s::pg_lsn,%s::pg_lsn,%s,%s,%s,%s,%s,%s)",
            (RUN_ID, FIX, APPLY_DELAY, gap_ms, entity, expect, val,
             wl or "0/0", replay, replay_ts, in_recovery, polls, fell_back,
             read_ms, read_now))

    return {"value": val, "stale": val != expect, "in_recovery": in_recovery,
            "polls": polls, "fell_back": fell_back}
