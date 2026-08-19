"""
Layer 2 · Topic 1 - What one cold HTTPS connection actually costs, measured.

Python because this is the client library every one of your services uses,
and because `ssl.MemoryBIO` lets us capture the exact ClientHello bytes
before they hit the wire -- which is how we answer the 2026 question
("does the ClientHello still fit in one packet?") with a real number from
this machine's OpenSSL rather than a blog post's.

Two parts:

  Part 1 - the staircase. DNS -> TCP -> TLS -> first byte, each phase timed
           separately against a real host, twice in a row in the same
           process. Watch which numbers drop on the second connection and
           which do not. Every phase that does NOT drop is a cost your
           connection pool would have skipped.

  Part 2 - the ClientHello. We drive a handshake through a memory BIO,
           grab the first flight of bytes, and print its size plus the key
           exchange groups it offers. If a post-quantum hybrid group
           (X25519MLKEM768, 0x11ec) is in there, the hello is ~1.2 KB
           larger and no longer fits in one 1460-byte TCP segment.

What to look for in the output: the "not reused" line in part 1, and
whether part 2's ClientHello size is above or below one segment.
Everything printed is measured now, on this machine. Nothing is canned.
"""
import os
import socket
import ssl
import struct
import sys
import time

HOST = os.environ.get("LAB_TLS_HOST", "example.com")
PORT = 443
TYPICAL_MSS = 1460  # 1500-byte Ethernet MTU minus 20 IP + 20 TCP header


# --------------------------------------------------------------------------
# Part 1 - phase timing
# --------------------------------------------------------------------------

def timed_https_get(host, port):
    """One complete cold HTTPS request, with a stopwatch between each phase.

    This is deliberately the raw socket version rather than httpx/requests.
    A client library would hide exactly the boundaries we are trying to see.
    """
    marks = {}
    t0 = time.perf_counter()

    infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    marks["dns"] = time.perf_counter() - t0

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.connect(infos[0][4])
    marks["tcp_connect"] = time.perf_counter() - t0

    context = ssl.create_default_context()
    tls = context.wrap_socket(sock, server_hostname=host)
    marks["tls_handshake"] = time.perf_counter() - t0

    request = f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
    tls.sendall(request.encode())
    tls.recv(1)
    marks["first_byte"] = time.perf_counter() - t0

    body = b""
    while True:
        chunk = tls.recv(65536)
        if not chunk:
            break
        body += chunk
    marks["complete"] = time.perf_counter() - t0

    details = {
        "peer_ip": infos[0][4][0],
        "tls_version": tls.version(),
        "cipher": tls.cipher()[0],
        "bytes": len(body) + 1,
    }
    tls.close()
    return marks, details


def report_phases(label, marks, details):
    print(f"  {label}")
    print(f"    peer={details['peer_ip']}  {details['tls_version']}  "
          f"{details['cipher']}  body={details['bytes']} bytes")
    previous = 0.0
    for phase in ("dns", "tcp_connect", "tls_handshake", "first_byte", "complete"):
        cumulative = marks[phase]
        step = cumulative - previous
        previous = cumulative
        print(f"    {phase:<15} +{step * 1000:8.2f} ms   (cumulative {cumulative * 1000:8.2f} ms)")


def part_one():
    print("PART 1 - the staircase, timed against a real host")
    print(f"  host: {HOST}:{PORT}")
    try:
        first_marks, first_details = timed_https_get(HOST, PORT)
    except OSError as exc:
        print(f"  SKIPPED - could not reach {HOST}: {type(exc).__name__}: {exc}")
        print("  (this half needs outbound internet; part 2 does not)")
        return
    report_phases("connection #1 (cold)", first_marks, first_details)

    second_marks, second_details = timed_https_get(HOST, PORT)
    report_phases("connection #2 (a brand new connection, not a reused one)",
                  second_marks, second_details)

    print("\n  What a warm pool would have skipped on connection #2:")
    tcp_cost = second_marks["tcp_connect"] - second_marks["dns"]
    tls_cost = second_marks["tls_handshake"] - second_marks["tcp_connect"]
    dns_cost = second_marks["dns"]
    setup = second_marks["tls_handshake"]
    total = second_marks["complete"]
    print(f"    dns            {dns_cost * 1000:8.2f} ms")
    print(f"    tcp handshake  {tcp_cost * 1000:8.2f} ms")
    print(f"    tls handshake  {tls_cost * 1000:8.2f} ms")
    print(f"    ---------------------------")
    print(f"    setup total    {setup * 1000:8.2f} ms  "
          f"= {setup / total * 100:.0f}% of this request's {total * 1000:.2f} ms")
    print("    A pooled client pays that once. A client built inside your")
    print("    request handler pays it on every single request.")


# --------------------------------------------------------------------------
# Part 2 - capture and dissect the ClientHello
# --------------------------------------------------------------------------

# Only the groups you are likely to meet. IANA "TLS Supported Groups".
GROUP_NAMES = {
    0x0017: "secp256r1",
    0x0018: "secp384r1",
    0x0019: "secp521r1",
    0x001d: "x25519",
    0x001e: "x448",
    0x0100: "ffdhe2048",
    0x0101: "ffdhe3072",
    0x0102: "ffdhe4096",
    0x0103: "ffdhe6144",
    0x0104: "ffdhe8192",
    0x11ec: "X25519MLKEM768  <-- post-quantum hybrid",
    0x6399: "X25519Kyber768Draft00 (legacy draft)",
}


def capture_client_hello(context, server_hostname):
    """Run a handshake against a memory BIO and stop after the first flight.

    No socket, no server, no network. `do_handshake()` writes the
    ClientHello into the outgoing BIO and then raises SSLWantReadError
    because there is no ServerHello to read -- at which point the exact
    bytes OpenSSL would have put on the wire are sitting in `outgoing`.
    """
    incoming = ssl.MemoryBIO()
    outgoing = ssl.MemoryBIO()
    tls_object = context.wrap_bio(incoming, outgoing, server_hostname=server_hostname)
    try:
        tls_object.do_handshake()
    except ssl.SSLWantReadError:
        pass
    return outgoing.read()


def parse_client_hello(record):
    """Return (offered_group_ids, key_share_group_ids) from a TLS record.

    A deliberately small hand-rolled parser: enough of RFC 8446 section 4.1.2
    to walk to the extensions and read the two that decide the hello's size.
    """
    if len(record) < 5 or record[0] != 0x16:
        raise ValueError("not a TLS handshake record")
    body = record[5:]
    if body[0] != 0x01:
        raise ValueError("not a ClientHello")

    cursor = 4                       # handshake type (1) + length (3)
    cursor += 2                      # legacy_version
    cursor += 32                     # random
    session_id_len = body[cursor]
    cursor += 1 + session_id_len
    cipher_suites_len = struct.unpack_from(">H", body, cursor)[0]
    cursor += 2 + cipher_suites_len
    compression_len = body[cursor]
    cursor += 1 + compression_len

    extensions_len = struct.unpack_from(">H", body, cursor)[0]
    cursor += 2
    end = cursor + extensions_len

    supported_groups = []
    key_share_groups = []
    while cursor + 4 <= end:
        ext_type, ext_len = struct.unpack_from(">HH", body, cursor)
        cursor += 4
        payload = body[cursor:cursor + ext_len]
        cursor += ext_len

        if ext_type == 10 and len(payload) >= 2:          # supported_groups
            list_len = struct.unpack_from(">H", payload, 0)[0]
            for offset in range(2, 2 + list_len, 2):
                supported_groups.append(struct.unpack_from(">H", payload, offset)[0])
        elif ext_type == 51 and len(payload) >= 2:        # key_share
            list_len = struct.unpack_from(">H", payload, 0)[0]
            inner = 2
            while inner + 4 <= 2 + list_len:
                group, key_len = struct.unpack_from(">HH", payload, inner)
                key_share_groups.append((group, key_len))
                inner += 4 + key_len

    return supported_groups, key_share_groups


def describe_group(group_id):
    return GROUP_NAMES.get(group_id, f"unknown (0x{group_id:04x})")


def part_two():
    print("\nPART 2 - the ClientHello this machine actually sends")
    print(f"  OpenSSL: {ssl.OPENSSL_VERSION}")

    context = ssl.create_default_context()
    record = capture_client_hello(context, HOST)
    supported, key_shares = parse_client_hello(record)

    print(f"\n  ClientHello record: {len(record)} bytes")
    print(f"  One TCP segment at a 1500-byte MTU carries {TYPICAL_MSS} bytes.")
    if len(record) > TYPICAL_MSS:
        segments = -(-len(record) // TYPICAL_MSS)
        print(f"  -> This hello spans {segments} segments. Middleboxes that assume")
        print("     'the ClientHello arrives in one packet' break on this.")
    else:
        print("  -> This hello fits in one segment.")

    print("\n  supported_groups offered:")
    for group in supported:
        print(f"    0x{group:04x}  {describe_group(group)}")

    print("\n  key_share entries actually carried (this is what costs bytes):")
    for group, key_len in key_shares:
        print(f"    0x{group:04x}  {describe_group(group):<40} {key_len} bytes of key material")

    has_pq = any(group == 0x11ec for group, _ in key_shares)
    print()
    if has_pq:
        print("  This build negotiates hybrid post-quantum key exchange by default.")
        print("  The ML-KEM public key alone is ~1184 bytes -- that is the whole")
        print("  reason the hello outgrew a single segment in 2025/26.")
    else:
        print("  This build does NOT offer a post-quantum hybrid group, so the")
        print("  hello is small. Hybrid PQ shipped in OpenSSL 3.5 / BoringSSL /")
        print("  current browsers; the OpenSSL version printed above is what")
        print("  decides it. Re-run this against a newer Python/OpenSSL and the")
        print("  byte count changes -- that is the point, and it is a real")
        print("  deployment variable, not a theoretical one.")


def main():
    print("=" * 78)
    print("What a connection actually costs")
    print("=" * 78)
    part_one()
    part_two()
    print()


if __name__ == "__main__":
    sys.exit(main())
