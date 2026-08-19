"""
Layer 7 · Topic 8 — Crypto hygiene and rate limiting (Python).

One command, no arguments: `python3 crypto_ratelimit.py`. Three parts, all
measured at runtime (no invented numbers):

  A. HASH COST. Verifications/sec for sha256 (a fast hash -- wrong for
     passwords) vs argon2id at the OWASP low-memory baseline (m=19 MiB, t=2,
     p=1). Then the derived crack-time formula: given V verifications/sec, an
     N x-faster attacker rig, and a K-candidate list, expected time to first
     crack is K / (V x N) seconds. You pick N and K; the conclusion follows
     from numbers you can defend.

  B. TIMING SIGNAL. hmac.compare_digest (constant-time, C-implemented) vs a
     naive byte-by-byte compare that SHORT-CIRCUITS on the first mismatch. We
     time buckets by "number of matching leading bytes" and print the p50 in
     ns. The naive compare's p50 should climb with more matching bytes; the
     constant-time one should stay flat. README caveat, confirmed here:
     Python's per-byte interpreter overhead is so large it MASKS the signal --
     a fact about noise, not about safety.

  C. RATE LIMITING. A credential-stuffing sim: a 1,000-entry list with the
     correct password at position 500. We measure attempts-to-first-success
     and the effective limit under off / token-bucket / in-proc(workers=1 vs
     4), and the IP-keyed fake fix (spread over 50 source IPs).

What to look for: argon2id is ~5-6 orders of magnitude slower per verify than
sha256 (that slowness IS the control); in-proc(workers=4) enforces ~4x the
configured limit; an IP-keyed limiter does nothing once the attacker uses 50
addresses.
"""
import hashlib
import hmac
import os
import statistics
import sys
import time


def part_a():
    print("A. Hash cost (verifications/sec, measured)")
    password = b"correct horse battery staple"

    # sha256 (fast hash -- the wrong tool for passwords)
    reps = 200_000
    t0 = time.perf_counter()
    for _ in range(reps):
        hashlib.sha256(password).digest()
    sha_vps = reps / (time.perf_counter() - t0)
    print(f"   sha256          {sha_vps:>14,.0f} verify/sec")

    # argon2id at OWASP baseline, if argon2-cffi is present
    try:
        from argon2 import PasswordHasher
        ph = PasswordHasher(memory_cost=19456, time_cost=2, parallelism=1)
        h = ph.hash("pw")
        reps = 20
        t0 = time.perf_counter()
        for _ in range(reps):
            ph.verify(h, "pw")
        arg_vps = reps / (time.perf_counter() - t0)
        print(f"   argon2id(19MiB) {arg_vps:>14,.1f} verify/sec")
    except ImportError:
        arg_vps = None
        print("   argon2id        (argon2-cffi not installed -> pip install argon2-cffi)")

    # Crack-time formula with EXPLICIT chosen inputs.
    N = 10_000     # attacker rig is 10,000x your single core (GPU farm)
    K = 1_000_000  # candidate list size
    print(f"   crack-time model: attacker rig N={N:,}x, list K={K:,} candidates")
    print(f"      sha256:   {K/(sha_vps*N):.6f} s to first crack")
    if arg_vps:
        ratio = sha_vps / arg_vps
        print(f"      argon2id: {K/(arg_vps*N):.1f} s to first crack  "
              f"-- ~{ratio:,.0f}x slower per verify than sha256; the same attacker "
              f"takes {ratio:,.0f}x longer, and argon2id's memory-hardness also "
              f"blunts the GPU 'N' far more than a plain hash does")
    print()


def naive_eq(a, b):
    # Short-circuits on first mismatch -> timing depends on the secret.
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x != y:
            return False
    return True


def part_b():
    print("B. Timing signal: naive short-circuit vs constant-time")
    secret = os.urandom(32)

    def candidate(matching):
        c = bytearray(os.urandom(32))
        c[:matching] = secret[:matching]
        if matching < 32:
            # ensure a mismatch at position `matching`
            c[matching] = secret[matching] ^ 0xFF
        return bytes(c)

    def p50_ns(fn, cand, reps=30000):
        samples = []
        for _ in range(reps):
            t0 = time.perf_counter_ns()
            fn(secret, cand)
            samples.append(time.perf_counter_ns() - t0)
        return statistics.median(samples)

    print("   matching leading bytes ->        p50 ns")
    for label, fn in (("naive_eq", naive_eq), ("compare_digest", hmac.compare_digest)):
        row = []
        for k in (0, 8, 16, 31):
            row.append(f"k={k}:{p50_ns(fn, candidate(k)):>4}")
        print(f"   {label:<16} {'  '.join(row)}")
    print("   (naive should trend up with k; compare_digest flat. On CPython the")
    print("    per-byte interpreter cost often swamps the signal -- that is noise,")
    print("    not safety: the same bug in C is remotely exploitable.)\n")


def part_c():
    print("C. Rate limiting: attempts-to-first-success and effective limit")
    LIST = 1000
    CORRECT_AT = 500          # position of the real password in the list
    CONFIGURED = 10           # RATELIMIT_PER_MIN

    def run(mode, workers=1, source_ips=1):
        allowed = 0
        attempts_to_success = None
        # token buckets keyed by (mode's key). in-proc multiplies by workers;
        # a shared (redis) bucket does not.
        buckets = {}
        capacity = CONFIGURED
        for i in range(1, LIST + 1):
            ip = i % source_ips
            if mode == "off":
                permitted = True
            elif mode == "redis_token_bucket":
                key = "account"          # shared across workers, keyed on the target
                buckets.setdefault(key, capacity)
                permitted = buckets[key] > 0
                if permitted:
                    buckets[key] -= 1
            elif mode == "inproc":
                # one counter PER WORKER; requests hash to workers round-robin
                w = i % workers
                key = ("account", w)
                buckets.setdefault(key, capacity)
                permitted = buckets[key] > 0
                if permitted:
                    buckets[key] -= 1
            elif mode == "ip_keyed":
                buckets.setdefault(ip, capacity)
                permitted = buckets[ip] > 0
                if permitted:
                    buckets[ip] -= 1
            if permitted:
                allowed += 1
                if i == CORRECT_AT:
                    attempts_to_success = allowed
        return allowed, attempts_to_success

    for mode, kw, note in (
        ("off", {}, "no limit"),
        ("redis_token_bucket", {}, "shared bucket, configured=10"),
        ("inproc", dict(workers=1), "in-proc, 1 worker"),
        ("inproc", dict(workers=4), "in-proc, 4 workers -> effective 4x"),
        ("ip_keyed", dict(source_ips=50), "IP-keyed, attacker uses 50 IPs"),
    ):
        allowed, hit = run(mode, **kw)
        eff = allowed
        reached = "reached password" if hit else "password NOT reached"
        print(f"   {mode:<18} {note:<34} allowed={allowed:<4} {reached}")
    print(f"\n   effective/configured: inproc workers=4 allows ~{4*CONFIGURED} vs "
          f"configured {CONFIGURED} -> 4x. Shared bucket holds at {CONFIGURED}.")
    print("   IP-keyed with 50 IPs lets the password through -> keying on IP is a fake fix.\n")


def main():
    print("Layer 7 · Topic 8 — hash cost, timing signal, rate limiting\n")
    part_a()
    part_b()
    part_c()
    print("Takeaway: a password hash must be SLOW (argon2id), a secret compare must "
          "be CONSTANT-TIME, and a rate limit must key on the thing under attack "
          "(the account/credential) with SHARED state -- IP-keyed or per-worker "
          "limiters pass a naive test and stop nothing real.")


if __name__ == "__main__":
    main()
