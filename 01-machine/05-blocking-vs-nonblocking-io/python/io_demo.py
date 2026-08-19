"""
Layer 1 - Blocking vs non-blocking IO, with a real socket.

A tiny local TCP server answers after an artificial 100ms delay (standing
in for a slow downstream service). We hit it N=20 times two ways:

  serial   -- one blocking socket.recv() at a time. The calling thread is
              genuinely parked by the OS between send and the response
              being available; it cannot do anything else while waiting.
  asyncio  -- N connections started together, each backed by a non-blocking
              socket registered with the OS's readiness API (epoll on
              Linux). Nothing waits idle: the event loop asks the kernel
              "wake me when ANY of these is ready" via a single epoll_wait
              call, instead of parking one thread per socket.

The server itself is thread-per-connection so it doesn't become the
bottleneck when 20 requests land at once -- this experiment is about the
client side.
"""
import asyncio
import socket
import threading
import time

HOST = "127.0.0.1"
RESPONSE_DELAY = 0.1
N = 20


def handle_conn(conn: socket.socket):
    with conn:
        conn.recv(1024)
        time.sleep(RESPONSE_DELAY)
        conn.sendall(b"ok")


def start_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, 0))
    server.listen(128)
    port = server.getsockname()[1]

    def accept_loop():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=handle_conn, args=(conn,), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    return server, port


def blocking_request(port):
    s = socket.create_connection((HOST, port))
    s.sendall(b"ping")
    s.recv(1024)
    s.close()


def bench_serial(port):
    start = time.perf_counter()
    for _ in range(N):
        blocking_request(port)
    return time.perf_counter() - start


async def async_request(port):
    reader, writer = await asyncio.open_connection(HOST, port)
    writer.write(b"ping")
    await writer.drain()
    await reader.read(1024)
    writer.close()
    await writer.wait_closed()


async def bench_concurrent(port):
    start = time.perf_counter()
    await asyncio.gather(*[async_request(port) for _ in range(N)])
    return time.perf_counter() - start


if __name__ == "__main__":
    server, port = start_server()
    t_serial = bench_serial(port)
    t_concurrent = asyncio.run(bench_concurrent(port))
    print(f"N={N} requests, {RESPONSE_DELAY*1000:.0f}ms server delay each")
    print(f"serial blocking sockets: {t_serial:.3f}s  (~{t_serial/N*1000:.0f}ms/req)")
    print(f"concurrent asyncio:      {t_concurrent:.3f}s")
