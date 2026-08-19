// Layer 7 · Topic 5 — OAuth2, OIDC, and PKCE end to end (Go).
//
// One command, stdlib only, no network: `go run pkce_flow.go`. The README's
// Go path (`golang.org/x/oauth2` + `coreos/go-oidc`) is the most manual of the
// three: AuthCodeURL takes state as a required positional argument, PKCE goes
// in via S256ChallengeOption, and ID-token verification is a separate
// oidc.Verifier you construct with the expected ClientID. Nothing is implicit,
// so nothing is silently wrong -- but every check is yours to write, and a Go
// OAuth handler is where you most often see state generated and never compared.
//
// Here the AS is modelled in-process; each scenario redeems an intercepted
// code and the measurement is tokens issued (0 or 1). replay-wrong-verifier
// sits above replay-no-verifier on purpose: comparing the verifier is the
// control, not generating it.
package main

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"fmt"
	"strings"
)

func b64url(b []byte) string { return base64.RawURLEncoding.EncodeToString(b) }
func s256(verifier string) string {
	h := sha256.Sum256([]byte(verifier))
	return b64url(h[:])
}
func rnd(n int) string {
	b := make([]byte, n)
	rand.Read(b)
	return b64url(b)
}

type record struct {
	challenge, method, redirectURI string
	issuedAt                       int
	used                           bool
}

type authServer struct {
	pkceMode, redirectMatch string
	codeTTL                 int
	singleUse               bool
	registeredRedirect      string
	codes                   map[string]*record
	now                     int
}

type cfg struct {
	pkceMode, redirectMatch string
	codeTTL                 int
	singleUse               bool
}

func newAS(c cfg) *authServer {
	if c.pkceMode == "" {
		c.pkceMode = "required"
	}
	if c.redirectMatch == "" {
		c.redirectMatch = "exact"
	}
	if c.codeTTL == 0 {
		c.codeTTL = 60
	}
	return &authServer{pkceMode: c.pkceMode, redirectMatch: c.redirectMatch,
		codeTTL: c.codeTTL, singleUse: c.singleUse,
		registeredRedirect: "https://app.test/cb", codes: map[string]*record{}}
}

func (a *authServer) authorize(redirectURI, challenge, method string) string {
	code := rnd(16)
	a.codes[code] = &record{challenge: challenge, method: method, redirectURI: redirectURI, issuedAt: a.now}
	return code
}

func (a *authServer) redirectOk(presented string) bool {
	if a.redirectMatch == "exact" {
		return presented == a.registeredRedirect
	}
	return strings.HasPrefix(presented, a.registeredRedirect) // the prefix bug
}

func (a *authServer) token(code, verifier, redirectURI string) (int, string) {
	rec := a.codes[code]
	if rec == nil {
		return 400, ""
	}
	if a.now-rec.issuedAt > a.codeTTL {
		return 400, ""
	}
	if rec.used && a.singleUse {
		return 400, ""
	}
	if !a.redirectOk(redirectURI) {
		return 400, ""
	}
	if a.pkceMode == "required" || (a.pkceMode == "optional" && rec.challenge != "") {
		if verifier == "" {
			return 400, ""
		}
		got := verifier
		if rec.method == "S256" {
			got = s256(verifier)
		}
		if got != rec.challenge {
			return 400, "" // THE control: wrong verifier rejected
		}
	}
	rec.used = true
	return 200, "access-token-" + rnd(8)
}

func scenario(name string, c cfg, run func(*authServer) (int, string)) {
	issued, detail := run(newAS(c))
	fmt.Printf("   %-22s tokens issued: %d   %s\n", name, issued, detail)
}

func issuedInt(tok string) int {
	if tok != "" {
		return 1
	}
	return 0
}

func main() {
	fmt.Println("Layer 7 · Topic 5 — OAuth2 / PKCE: replay, downgrade, reuse, redirect")
	fmt.Println()
	fmt.Println("  Attacker holds an intercepted authorization code and tries to redeem it:")

	scenario("happy-path", cfg{}, func(a *authServer) (int, string) {
		v := rnd(32)
		code := a.authorize("https://app.test/cb", s256(v), "S256")
		_, tok := a.token(code, v, "https://app.test/cb")
		return issuedInt(tok), "legit client with the matching verifier -> OK"
	})
	scenario("replay-no-verifier", cfg{}, func(a *authServer) (int, string) {
		v := rnd(32)
		code := a.authorize("https://app.test/cb", s256(v), "S256")
		_, tok := a.token(code, "", "https://app.test/cb")
		return issuedInt(tok), "no code_verifier -> AS must refuse"
	})
	scenario("replay-wrong-verifier", cfg{}, func(a *authServer) (int, string) {
		v := rnd(32)
		code := a.authorize("https://app.test/cb", s256(v), "S256")
		_, tok := a.token(code, rnd(32), "https://app.test/cb")
		return issuedInt(tok), "wrong verifier -> proves the AS VERIFIES"
	})
	scenario("replay-no-pkce", cfg{pkceMode: "off"}, func(a *authServer) (int, string) {
		code := a.authorize("https://app.test/cb", "", "S256")
		_, tok := a.token(code, "", "https://app.test/cb")
		return issuedInt(tok), "PKCE off -> a stolen code alone is enough"
	})
	scenario("downgrade-plain", cfg{pkceMode: "optional"}, func(a *authServer) (int, string) {
		secret := "attacker-chosen-value"
		code := a.authorize("https://app.test/cb", secret, "plain")
		_, tok := a.token(code, secret, "https://app.test/cb")
		return issuedInt(tok), "method=plain lets the attacker pick both halves"
	})
	scenario("code-reuse", cfg{singleUse: true}, func(a *authServer) (int, string) {
		v := rnd(32)
		code := a.authorize("https://app.test/cb", s256(v), "S256")
		a.token(code, v, "https://app.test/cb")
		_, tok := a.token(code, v, "https://app.test/cb")
		return issuedInt(tok), "second use of a single-use code -> refused"
	})
	scenario("code-expiry", cfg{codeTTL: 60}, func(a *authServer) (int, string) {
		v := rnd(32)
		code := a.authorize("https://app.test/cb", s256(v), "S256")
		a.now = 61
		_, tok := a.token(code, v, "https://app.test/cb")
		return issuedInt(tok), "redeemed after 61s (ttl=60) -> expired"
	})
	redirectRun := func(a *authServer) (int, string) {
		v := rnd(32)
		code := a.authorize("https://app.test/cb.attacker.test", s256(v), "S256")
		_, tok := a.token(code, v, "https://app.test/cb.attacker.test")
		return issuedInt(tok), fmt.Sprintf("redeem cb.attacker.test under %s matching", a.redirectMatch)
	}
	scenario("redirect-prefix", cfg{redirectMatch: "prefix"}, redirectRun)
	scenario("redirect-exact ", cfg{redirectMatch: "exact"}, redirectRun)

	fmt.Println("\nRead: PKCE (S256) makes an intercepted code useless without the " +
		"verifier the attacker never saw -- but only if the AS COMPARES it " +
		"(replay-wrong-verifier must be 0) and refuses plain downgrades. Single-use, " +
		"short TTL and exact redirect matching close the rest.")
}
