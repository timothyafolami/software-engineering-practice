"""
Layer 7 · Topic 6 — SSRF: validate the connection, not the string (Python).

One command, no network: `python3 ssrf.py`. SSRF is a runtime topic -- the
subject is DNS resolution and socket connect -- and the bug this file makes
concrete is validating the URL STRING while the socket connects to a
different ADDRESS. Three modes over a fixed payload set:

  vulnerable        connect to whatever was asked.
  string_blocklist  refuse if the host string is one of {localhost, 127.0.0.1,
                    169.254.169.254}. Defeated by every encoding below.
  resolve_and_pin   parse the real host (after any userinfo@), RESOLVE it to an
                    IP, and refuse loopback / private / link-local / unspecified
                    -- the deny set the lab README describes: "loopback,
                    RFC-1918, link-local, plus whatever this environment's
                    metadata address happens to be."

Python note (README): `requests` follows redirects by DEFAULT; `httpx` does
not -- knowing which of your two HTTP clients does is the point. Neither
exposes a resolver hook, so pinning means `socket.getaddrinfo` yourself and
connecting to the literal IP with an explicit Host header. The common wrong
pattern is a regex over the URL string, wrong for the reason above.

What to look for: string_blocklist ALLOWs the internal targets via decimal,
`0`, IPv6 and userinfo encodings (the bypasses); resolve_and_pin BLOCKs them
all, because it checks the resolved address, not the text.
"""
import ipaddress
from urllib.parse import urlsplit

# A stand-in resolver: real DNS for these lab names does not exist on this
# host, so we model it. resolve_and_pin uses this; the point is that it maps
# to an ADDRESS, which is what must be checked.
FAKE_DNS = {
    "internal-admin": "10.7.0.10",
    "metadata": "10.7.0.169",
    "allowed.test": "93.184.216.34",     # a public address
    "a.rebind.lab.test": "10.7.0.10",    # rebinding: resolves to the private target
    "localhost": "127.0.0.1",
}

PAYLOADS = [
    ("http://internal-admin:8000/secrets",            "plain internal reach"),
    ("http://10.7.0.169/latest/meta-data/iam/...",    "cloud metadata / credential theft"),
    ("http://0/secrets",                              "0 == 0.0.0.0 (this host)"),
    ("http://2130706433/",                            "decimal form of 127.0.0.1"),
    ("http://[::1]:8000/",                            "IPv6 loopback"),
    ("http://ok.test@10.7.0.10/secrets",              "userinfo confusion (real host after @)"),
    ("http://a.rebind.lab.test/secrets",              "DNS rebinding (TOCTOU)"),
]

STRING_DENY = {"localhost", "127.0.0.1", "169.254.169.254"}


def host_of(url):
    """The host the SOCKET will use: after userinfo, brackets stripped."""
    parts = urlsplit(url)
    return parts.hostname or ""  # urlsplit already drops userinfo and [] brackets


def canonical_ip(host):
    """Resolve/canonicalize a host to an IP string, or None if not an address
    and not in the fake resolver."""
    if host in FAKE_DNS:
        host = FAKE_DNS[host]
    # Numeric forms: decimal (2130706433), plain int like 0, dotted, IPv6.
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if host.isdigit():  # e.g. "0", "2130706433"
        return str(ipaddress.ip_address(int(host)))
    return None


def is_denied_ip(ip_str):
    ip = ipaddress.ip_address(ip_str)
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_unspecified or ip.is_reserved)


def verdict_string_blocklist(url):
    # Naive: check the raw host substring against the denylist.
    raw = urlsplit(url).netloc.lower()
    host = host_of(url).lower()
    blocked = host in STRING_DENY or any(d in raw for d in STRING_DENY)
    return "BLOCK" if blocked else "ALLOW", ""


def verdict_resolve_and_pin(url):
    host = host_of(url)
    ip = canonical_ip(host)
    if ip is None:
        return "BLOCK", "unresolvable"   # fail closed on unknown names
    return ("BLOCK" if is_denied_ip(ip) else "ALLOW"), ip


def imds_note():
    print("\nIMDS v1 vs v2 (metadata service):")
    print("   v1: a plain GET returns credentials                 -> bytes returned > 0")
    print("   v2: the same GET without a PUT-obtained token -> 401 -> bytes returned = 0")
    print("   v2 is defence-in-depth: it does not fix SSRF, it raises the bar so a")
    print("   pure GET-only SSRF cannot read credentials without also forging a PUT.")


def main():
    print("Layer 7 · Topic 6 — SSRF: string blocklist vs resolve-and-pin\n")
    print(f"   {'payload':<44}{'blocklist':<11}{'resolve+pin':<13}resolved")
    reached_blocklist = reached_pin = 0
    for url, _desc in PAYLOADS:
        v1, _ = verdict_string_blocklist(url)
        v2, ip = verdict_resolve_and_pin(url)
        reached_blocklist += v1 == "ALLOW"
        reached_pin += v2 == "ALLOW"
        print(f"   {url:<44}{v1:<11}{v2:<13}{ip}")
    print(f"\n   internal targets reached -- string_blocklist: {reached_blocklist}/{len(PAYLOADS)}"
          f"   resolve_and_pin: {reached_pin}/{len(PAYLOADS)}")
    imds_note()
    print("\nRead: the string blocklist is defeated by any encoding it did not "
          "enumerate -- 0, decimal, IPv6, userinfo -- because the STRING is not "
          "the ADDRESS. resolve_and_pin checks the resolved IP, so all internal "
          "reaches fail closed. For rebinding, the real fix pins the RESOLVED "
          "address and connects to THAT, closing the window between check and "
          "connect; re-resolving after validating is the bug.")


if __name__ == "__main__":
    main()
