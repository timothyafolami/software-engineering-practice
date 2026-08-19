"""
Layer 2 · Topic 7 - Python's contribution to the SYN table.

One reused httpx.Client, LAB_REQUESTS requests. Run by
pools_as_advertised.py, which counts the connections from the server side;
runnable on its own too, against any URL:

    LAB_URL=http://127.0.0.1:8000/work python3 syn_client.py
"""
import os
import sys
import time

URL = os.environ.get("LAB_URL", "http://127.0.0.1:8000/work")
N = int(os.environ.get("LAB_REQUESTS", "30"))

try:
    import httpx
except ImportError:
    sys.exit("httpx not installed: pip install httpx")

t0 = time.perf_counter()
with httpx.Client(timeout=10.0) as client:      # ONE client, kept alive
    for _ in range(N):
        client.get(URL).read()
print(f"httpx.Client reused across {N} requests in "
      f"{(time.perf_counter() - t0) * 1000:.0f} ms")
