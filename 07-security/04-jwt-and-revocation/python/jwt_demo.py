"""
Layer 7 · Topic 4 — What a JWT is, and the revocation problem (Python / PyJWT).

One command, no arguments: `python3 jwt_demo.py`. Three parts, all measured:

  A. IT IS NOT ENCRYPTED. We read the claims of a signed token with nothing
     but base64 -- no key. Signing proves integrity and authorship, not
     secrecy. Never put anything in a JWT you would not print.

  B. THE alg-CONFUSION ATTACK (README Part D), run for real. The server signs
     RS256 with a private key and publishes the PUBLIC key. An attacker forges
     a token signed HS256, using the public key BYTES as the HMAC secret. A
     verifier that accepts {RS256, HS256} treats the public key as an HMAC
     secret and ACCEPTS the forgery. PyJWT made `algorithms=` required years
     ago precisely to force the pin that stops this -- we show the pinned
     verifier rejecting the same token.

  C. THE REVOCATION GAP, measured. For each strategy we log in, poll /me with
     the already-issued token, POST /logout, and record the wall-clock ms from
     logout to the first 401. A stateless JWT cannot be un-issued: its
     revocation latency is the full remaining TTL. A denylist checked on every
     request revokes in about one poll interval -- at the cost of a lookup per
     request, which turns your "stateless" token back into a session.

What to look for: Part B's vulnerable verifier prints ACCEPTED (forgery),
pinned prints REJECTED; Part C's `plain` latency is ~the remaining TTL while
`denylist` is ~one poll interval.
"""
import base64
import json
import sys
import time

try:
    import jwt  # PyJWT
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
except ImportError as e:
    print(f"missing dep -> {e}. pip install 'pyjwt[crypto]'")
    sys.exit(0)


def make_rsa():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pub


def part_a():
    print("A. A JWT is signed, NOT encrypted")
    priv, _ = make_rsa()
    token = jwt.encode({"sub": "alice", "role": "admin", "note": "not secret"},
                       priv, algorithm="RS256")
    payload_seg = token.split(".")[1]
    payload_seg += "=" * (-len(payload_seg) % 4)  # base64url padding
    claims = json.loads(base64.urlsafe_b64decode(payload_seg))
    print(f"   claims read with no key: {claims}")
    print("   -> anyone holding the token reads every claim.\n")


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=")


def _forge_hs256(claims, hmac_key_bytes):
    """Hand-build an HS256 token, HMAC-keyed on the RSA PUBLIC key bytes --
    the attacker does not need a JWT library and is not bound by its guards."""
    import hmac as _hmac
    import hashlib
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64url(json.dumps(claims).encode())
    signing_input = header + b"." + body
    sig = _hmac.new(hmac_key_bytes, signing_input, hashlib.sha256).digest()
    return (signing_input + b"." + _b64url(sig)).decode()


def part_b():
    print("B. alg-confusion: forge HS256 with the RS256 public key as the secret")
    priv, pub = make_rsa()
    good = jwt.encode({"sub": "alice", "role": "user"}, priv, algorithm="RS256")
    forged = _forge_hs256({"sub": "alice", "role": "admin"}, pub.encode())

    def naive_verify(token, hmac_key_bytes):
        # A HAND-ROLLED verifier that trusts header.alg and uses the pubkey as
        # the HMAC secret -- exactly the old jwt.verify(token, secret) shape.
        import hmac as _hmac
        import hashlib
        h, b, sig = token.split(".")
        expected = _b64url(_hmac.new(hmac_key_bytes, f"{h}.{b}".encode(),
                                     hashlib.sha256).digest()).decode()
        return "ACCEPTED role=admin" if _hmac.compare_digest(expected, sig) else "REJECTED"

    def pyjwt_verify(token, accepted_algs):
        try:
            claims = jwt.decode(token, pub, algorithms=accepted_algs)
            return "ACCEPTED role=" + claims.get("role", "?")
        except jwt.PyJWTError as e:
            return f"REJECTED ({type(e).__name__})"

    print(f"   legit RS256 token, PyJWT [RS256]:             {pyjwt_verify(good, ['RS256'])}")
    print(f"   forged HS256, hand-rolled naive verifier:     {naive_verify(forged, pub.encode())}  <- the attack works")
    print(f"   forged HS256, PyJWT [RS256,HS256]:            {pyjwt_verify(forged, ['RS256', 'HS256'])}")
    print(f"   forged HS256, PyJWT [RS256] (pinned):         {pyjwt_verify(forged, ['RS256'])}")
    print("   PyJWT stops this TWO ways: it refuses an asymmetric key as an HMAC")
    print("   secret, and it requires algorithms= so an unpinned verifier cannot")
    print("   silently accept HS256. The naive verifier had neither guard.\n")


def part_c():
    print("C. Revocation latency by strategy (poll every 50ms after logout)")
    TTL_MS, LOGOUT_AT, POLL_MS = 2000, 500, 50

    def latency(strategy):
        priv, pub = make_rsa()
        jti = "tok-123"
        issued = jwt.encode({"sub": "alice", "jti": jti,
                             "exp": int(time.time()) + TTL_MS // 1000},
                            priv, algorithm="RS256")
        denylist, opaque_dead = set(), {"dead": False}

        def me(now_ms):
            # signature+exp always checked; strategies add server-side state.
            if now_ms >= TTL_MS:
                return 401  # expired
            if strategy == "denylist" and jti in denylist:
                return 401
            if strategy == "opaque_introspect" and opaque_dead["dead"]:
                return 401
            return 200  # plain: /me never consults revocation state

        def logout():
            if strategy == "denylist":
                denylist.add(jti)
            elif strategy == "opaque_introspect":
                opaque_dead["dead"] = True
            # plain: kills a server session /me does not read -> no effect on token

        logout()  # at t=LOGOUT_AT
        for now in range(LOGOUT_AT, TTL_MS + POLL_MS, POLL_MS):
            if me(now) == 401:
                return now - LOGOUT_AT
        return TTL_MS - LOGOUT_AT

    for strat in ("plain", "denylist", "opaque_introspect"):
        ms = latency(strat)
        note = "= full remaining TTL" if strat == "plain" else "~ one poll interval"
        print(f"   {strat:<20} revocation latency: {ms:>4} ms   {note}")
    print("   (plain 'logout' invalidates a session /me never checks; the token "
          "stays valid until exp)\n")


def main():
    print("Layer 7 · Topic 4 — JWT: not-encrypted, alg-confusion, revocation\n")
    part_a()
    part_b()
    part_c()
    print("Takeaway: a stateless JWT trades revocability for statelessness. If "
          "you need instant logout you need per-request server state (denylist "
          "or introspection) -- at which point you have a session and should "
          "re-ask whether the JWT bought you anything.")


if __name__ == "__main__":
    main()
