"""
Layer 5 lab - the uvicorn entrypoint.

WHY THIS EXISTS RATHER THAN A `uvicorn` COMMAND LINE
  Two of the environment variables in ../README.md are uvicorn's own, and
  they are named there because topic 5 changes them:

    UVICORN_BACKLOG             the kernel accept queue (listen(2)'s backlog).
                                Connections past the TCP handshake but not
                                yet accepted. Invisible to every application
                                metric you have, which is exactly why a
                                request can be four seconds late before your
                                first line of Python runs.
    UVICORN_LIMIT_CONCURRENCY   uvicorn's own crude static shedder: past this
                                many concurrent requests it returns 503
                                without asking anyone. Compare it against
                                SHED_MODE=static, which waits SHED_WAIT_MS
                                first and tells the client to come back.

  Passing them through a Python entrypoint keeps them settable from the same
  compose environment block as everything else.
"""
from __future__ import annotations

import os

import uvicorn

from .config import config

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        backlog=int(config.get("UVICORN_BACKLOG")),
        limit_concurrency=config.get("UVICORN_LIMIT_CONCURRENCY"),
        access_log=False,
        log_level=os.environ.get("LOG_LEVEL", "warning"),
    )
