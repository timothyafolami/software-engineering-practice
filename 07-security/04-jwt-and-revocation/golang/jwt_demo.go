// Layer 7 · Topic 4 — What a JWT is, and the revocation problem (Go / golang-jwt).
//
// `GOFLAGS=-mod=mod GOPROXY=off go run jwt_demo.go` (golang-jwt/v5 is in the
// module cache). golang-jwt is unusually honest API design: verification takes
// a Keyfunc that RECEIVES the parsed token, and the documented-correct
// implementation asserts token.Method.(*jwt.SigningMethodRSA) inside it before
// returning a key. It hands you the attacker-controlled algorithm and makes
// you write the check -- so Go code that skips the assertion is VISIBLY missing
// a line, not relying on an absent default.
//
// Three parts: (A) a JWT is signed, not encrypted; (B) forge HS256 with the
// RS256 public key as the HMAC secret; a Keyfunc that returns the key for any
// method accepts the forgery, a Keyfunc that asserts the method rejects it;
// (C) revocation latency per strategy.
package main

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"

	"github.com/golang-jwt/jwt/v5"
)

func mustRSA() *rsa.PrivateKey {
	k, _ := rsa.GenerateKey(rand.Reader, 2048)
	return k
}

func pubPEM(k *rsa.PrivateKey) []byte {
	der, _ := x509.MarshalPKIXPublicKey(&k.PublicKey)
	return pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der})
}

func partA() {
	fmt.Println("A. A JWT is signed, NOT encrypted")
	k := mustRSA()
	tok := jwt.NewWithClaims(jwt.SigningMethodRS256, jwt.MapClaims{
		"sub": "alice", "role": "admin", "note": "not secret"})
	s, _ := tok.SignedString(k)
	parts := splitDot(s)
	raw, _ := base64.RawURLEncoding.DecodeString(parts[1])
	var claims map[string]any
	json.Unmarshal(raw, &claims)
	fmt.Printf("   claims read with no key: %v\n", claims)
	fmt.Println("   -> anyone holding the token reads every claim.")
	fmt.Println()
}

func splitDot(s string) []string {
	out, cur := []string{}, ""
	for _, c := range s {
		if c == '.' {
			out = append(out, cur)
			cur = ""
		} else {
			cur += string(c)
		}
	}
	return append(out, cur)
}

func partB() {
	fmt.Println("B. alg-confusion: forge HS256 with the RS256 public key as the secret")
	k := mustRSA()
	pub := pubPEM(k)

	good, _ := jwt.NewWithClaims(jwt.SigningMethodRS256,
		jwt.MapClaims{"sub": "alice", "role": "user"}).SignedString(k)
	// Attacker signs HS256 using the PUBLIC key PEM bytes as the HMAC secret.
	forged, _ := jwt.NewWithClaims(jwt.SigningMethodHS256,
		jwt.MapClaims{"sub": "alice", "role": "admin"}).SignedString(pub)

	// VULNERABLE Keyfunc: returns a usable key for whatever method the token
	// claims -- HMAC secret for HS*, public key for RS*.
	vulnerable := func(t *jwt.Token) (any, error) {
		if _, ok := t.Method.(*jwt.SigningMethodHMAC); ok {
			return pub, nil // treats the public key as an HMAC secret
		}
		return &k.PublicKey, nil
	}
	// FIXED Keyfunc: assert the method is RSA before returning a key.
	fixed := func(t *jwt.Token) (any, error) {
		if _, ok := t.Method.(*jwt.SigningMethodRSA); !ok {
			return nil, fmt.Errorf("unexpected alg %v", t.Header["alg"])
		}
		return &k.PublicKey, nil
	}

	report := func(label, token string, kf jwt.Keyfunc) {
		parsed, err := jwt.Parse(token, kf)
		if err != nil || !parsed.Valid {
			fmt.Printf("   %-46s REJECTED (%v)\n", label, err)
			return
		}
		role := parsed.Claims.(jwt.MapClaims)["role"]
		fmt.Printf("   %-46s ACCEPTED role=%v\n", label, role)
	}
	report("legit RS256, fixed Keyfunc:", good, fixed)
	report("forged HS256, vulnerable Keyfunc:", forged, vulnerable)
	report("forged HS256, fixed Keyfunc (asserts RSA):", forged, fixed)
	fmt.Println("   The fixed Keyfunc asserts t.Method is RSA before returning a key,")
	fmt.Println("   so the attacker cannot swap in HMAC. The library hands you the alg;")
	fmt.Println("   the one-line assertion is yours to write.")
	fmt.Println()
}

func partC() {
	fmt.Println("C. Revocation latency by strategy (poll every 50ms after logout)")
	const ttl, logoutAt, poll = 2000, 500, 50
	latency := func(strategy string) int {
		jti := "tok-123"
		denylist := map[string]bool{}
		opaqueDead := false
		me := func(now int) int {
			if now >= ttl {
				return 401
			}
			if strategy == "denylist" && denylist[jti] {
				return 401
			}
			if strategy == "opaque_introspect" && opaqueDead {
				return 401
			}
			return 200
		}
		switch strategy {
		case "denylist":
			denylist[jti] = true
		case "opaque_introspect":
			opaqueDead = true
		}
		for now := logoutAt; now <= ttl; now += poll {
			if me(now) == 401 {
				return now - logoutAt
			}
		}
		return ttl - logoutAt
	}
	for _, s := range []string{"plain", "denylist", "opaque_introspect"} {
		note := "~ one poll interval"
		if s == "plain" {
			note = "= full remaining TTL"
		}
		fmt.Printf("   %-20s revocation latency: %4d ms   %s\n", s, latency(s), note)
	}
	fmt.Println("   (plain 'logout' invalidates a session /me never checks)")
	fmt.Println()
}

func main() {
	fmt.Println("Layer 7 · Topic 4 — JWT: not-encrypted, alg-confusion, revocation")
	fmt.Println()
	partA()
	partB()
	partC()
	fmt.Println("Takeaway: a stateless JWT trades revocability for statelessness. " +
		"Instant logout needs per-request server state -- a session by another name.")
}
