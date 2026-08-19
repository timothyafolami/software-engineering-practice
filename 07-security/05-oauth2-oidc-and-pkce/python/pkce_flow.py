"""
Layer 7 · Topic 5 — OAuth2, OIDC, and PKCE end to end (Python).

One command, no arguments, no network: `python3 pkce_flow.py`. The protocol
is HTTP redirects and one SHA-256, so it runs the same in every runtime; the
whole flow is modelled in-process by a minimal `AuthServer` (the `idp`) and a
scripted attacker who intercepts the authorization code from the redirect
`Location` -- exactly the attacker's real position (a referrer, a proxy log,
a mobile URL-scheme hijack).

Each scenario configures the AuthServer and tries to redeem an intercepted
code. The measurement is tokens issued (0 or 1), not an opinion:

  replay-no-verifier    code, no code_verifier          PKCE required
  replay-wrong-verifier code + a random verifier         PKCE required
  replay-no-pkce        code, no verifier                PKCE off
  downgrade-plain       attacker's own plain challenge   PKCE optional
  code-reuse            legit exchange, then same code   single-use
  code-expiry           redeem after CODE_TTL            ttl=60
  redirect-prefix       cb vs cb.attacker.test           prefix vs exact
  no-state              deliver attacker's code to victim client-side check

The load-bearing pair is replay-WRONG-verifier ABOVE replay-no-verifier:
generating a verifier proves nothing; the control is the AS COMPARING it, so
a wrong verifier must be REJECTED. A PKCE that is sent but not verified passes
every wire check and is decorative.
"""
import base64
import hashlib
import os
import secrets


def b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def s256(verifier):
    return b64url(hashlib.sha256(verifier.encode()).digest())


class AuthServer:
    """Minimal /authorize + /token. Config flags mirror the lab's env."""
    def __init__(self, pkce_mode="required", code_ttl=60,
                 single_use=True, redirect_match="exact"):
        self.pkce_mode = pkce_mode          # required | optional | off
        self.code_ttl = code_ttl
        self.single_use = single_use
        self.redirect_match = redirect_match  # exact | prefix
        self.registered_redirect = "https://app.test/cb"
        self.codes = {}                     # code -> record
        self.now = 0

    def authorize(self, redirect_uri, state, code_challenge=None, method="S256"):
        code = secrets.token_urlsafe(16)
        self.codes[code] = dict(challenge=code_challenge, method=method,
                                redirect_uri=redirect_uri, issued_at=self.now,
                                used=False)
        # The AS 302s to redirect_uri?code=...&state=...; the attacker reads it.
        return code, state

    def _redirect_ok(self, registered, presented):
        if self.redirect_match == "exact":
            return presented == registered
        return presented.startswith(registered)  # the prefix bug

    def token(self, code, code_verifier=None, redirect_uri=None):
        """Returns (status, token_or_none)."""
        rec = self.codes.get(code)
        if rec is None:
            return 400, None  # unknown code
        if self.now - rec["issued_at"] > self.code_ttl:
            return 400, None  # expired
        if rec["used"] and self.single_use:
            return 400, None  # replay of a single-use code
        # RFC: the presented redirect_uri must match the REGISTERED one (not
        # merely the attacker-supplied value echoed back from /authorize).
        if not self._redirect_ok(self.registered_redirect, redirect_uri):
            return 400, None  # redirect_uri mismatch
        # PKCE verification
        if self.pkce_mode == "required" or (self.pkce_mode == "optional" and rec["challenge"]):
            if not code_verifier:
                return 400, None  # required but absent
            expected = rec["challenge"]
            got = s256(code_verifier) if rec["method"] == "S256" else code_verifier
            if got != expected:
                return 400, None  # THE control: wrong verifier rejected
        rec["used"] = True
        return 200, "access-token-" + secrets.token_hex(8)


def scenario(name, cfg, run):
    a = AuthServer(**cfg)
    issued, detail = run(a)
    print(f"   {name:<22} tokens issued: {issued}   {detail}")


def main():
    print("Layer 7 · Topic 5 — OAuth2 / PKCE: replay, downgrade, reuse, redirect\n")
    print("  Attacker holds an intercepted authorization code and tries to redeem it:")

    # Baseline happy path (the legitimate client has the verifier).
    def happy(a):
        verifier = secrets.token_urlsafe(32)
        code, _ = a.authorize("https://app.test/cb", "xyz", s256(verifier))
        st, tok = a.token(code, verifier, "https://app.test/cb")
        return (1 if tok else 0), "legit client with the matching verifier -> OK"
    scenario("happy-path", dict(pkce_mode="required"), happy)

    def replay_no_verifier(a):
        verifier = secrets.token_urlsafe(32)
        code, _ = a.authorize("https://app.test/cb", "xyz", s256(verifier))
        st, tok = a.token(code, None, "https://app.test/cb")  # attacker has no verifier
        return (1 if tok else 0), "no code_verifier -> AS must refuse"
    scenario("replay-no-verifier", dict(pkce_mode="required"), replay_no_verifier)

    def replay_wrong_verifier(a):
        verifier = secrets.token_urlsafe(32)
        code, _ = a.authorize("https://app.test/cb", "xyz", s256(verifier))
        st, tok = a.token(code, secrets.token_urlsafe(32), "https://app.test/cb")  # random
        return (1 if tok else 0), "wrong verifier -> the control that proves the AS VERIFIES"
    scenario("replay-wrong-verifier", dict(pkce_mode="required"), replay_wrong_verifier)

    def replay_no_pkce(a):
        code, _ = a.authorize("https://app.test/cb", "xyz", None)
        st, tok = a.token(code, None, "https://app.test/cb")
        return (1 if tok else 0), "PKCE off -> a stolen code alone is enough"
    scenario("replay-no-pkce", dict(pkce_mode="off"), replay_no_pkce)

    def downgrade_plain(a):
        # Attacker runs /authorize with method=plain and their OWN challenge.
        attacker_secret = "attacker-chosen-value"
        code, _ = a.authorize("https://app.test/cb", "xyz", attacker_secret, method="plain")
        st, tok = a.token(code, attacker_secret, "https://app.test/cb")
        return (1 if tok else 0), "method=plain lets the attacker pick both halves"
    scenario("downgrade-plain", dict(pkce_mode="optional"), downgrade_plain)

    def code_reuse(a):
        verifier = secrets.token_urlsafe(32)
        code, _ = a.authorize("https://app.test/cb", "xyz", s256(verifier))
        a.token(code, verifier, "https://app.test/cb")            # legit first use
        st, tok = a.token(code, verifier, "https://app.test/cb")  # replay
        return (1 if tok else 0), "second use of a single-use code -> refused"
    scenario("code-reuse", dict(pkce_mode="required", single_use=True), code_reuse)

    def code_expiry(a):
        verifier = secrets.token_urlsafe(32)
        code, _ = a.authorize("https://app.test/cb", "xyz", s256(verifier))
        a.now = 61  # past CODE_TTL_SECONDS=60
        st, tok = a.token(code, verifier, "https://app.test/cb")
        return (1 if tok else 0), "redeemed after 61s (ttl=60) -> expired"
    scenario("code-expiry", dict(pkce_mode="required", code_ttl=60), code_expiry)

    def redirect_prefix(a):
        verifier = secrets.token_urlsafe(32)
        # Registered cb is https://app.test/cb; attacker redeems with a prefix match.
        code, _ = a.authorize("https://app.test/cb.attacker.test", "xyz", s256(verifier))
        st, tok = a.token(code, verifier, "https://app.test/cb.attacker.test")
        return (1 if tok else 0), f"redeem cb.attacker.test under {a.redirect_match} matching"
    scenario("redirect-prefix", dict(pkce_mode="required", redirect_match="prefix"), redirect_prefix)
    scenario("redirect-exact ", dict(pkce_mode="required", redirect_match="exact"), redirect_prefix)

    print("\nRead: PKCE (S256) makes an intercepted code useless without the "
          "verifier the attacker never saw -- but ONLY if the AS compares it "
          "(replay-wrong-verifier must be 0) and refuses method=plain downgrades. "
          "Single-use + short TTL + exact redirect matching close the rest. Every "
          "'1' above is a homegrown-AS bug the RFC 9700 checklist names.")


if __name__ == "__main__":
    main()
