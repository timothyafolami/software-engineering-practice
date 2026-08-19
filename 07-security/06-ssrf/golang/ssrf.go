// Layer 7 · Topic 6 — SSRF: validate the connection, not the string (Go).
//
// One command, stdlib only, no network: `go run ssrf.go`.
// Go is the most ergonomic correct answer in the lab (README):
// http.Client.CheckRedirect receives every hop and can re-run your validator,
// and http.Transport.DialContext receives the RESOLVED address at connect
// time -- so Go lets you validate the ACTUAL address being connected to,
// closing the rebinding window structurally rather than by pinning a string.
// This program models the validator those hooks would call.
//
// Three modes over a fixed payload set; the finding is that string_blocklist
// ALLOWs every internal target via an encoding it did not enumerate, while
// resolve_and_pin BLOCKs them all by checking the resolved IP.
package main

import (
	"fmt"
	"net"
	"net/url"
	"strconv"
	"strings"
)

var fakeDNS = map[string]string{
	"internal-admin":    "10.7.0.10",
	"metadata":          "10.7.0.169",
	"allowed.test":      "93.184.216.34",
	"a.rebind.lab.test": "10.7.0.10",
	"localhost":         "127.0.0.1",
}

var stringDeny = []string{"localhost", "127.0.0.1", "169.254.169.254"}

type payload struct{ url, desc string }

var payloads = []payload{
	{"http://internal-admin:8000/secrets", "plain internal reach"},
	{"http://10.7.0.169/latest/meta-data/iam/...", "cloud metadata / credential theft"},
	{"http://0/secrets", "0 == 0.0.0.0 (this host)"},
	{"http://2130706433/", "decimal form of 127.0.0.1"},
	{"http://[::1]:8000/", "IPv6 loopback"},
	{"http://ok.test@10.7.0.10/secrets", "userinfo confusion (real host after @)"},
	{"http://a.rebind.lab.test/secrets", "DNS rebinding (TOCTOU)"},
}

func hostOf(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	return u.Hostname() // drops userinfo and [] brackets
}

func canonicalIP(host string) net.IP {
	if mapped, ok := fakeDNS[host]; ok {
		host = mapped
	}
	if ip := net.ParseIP(host); ip != nil {
		return ip
	}
	if n, err := strconv.ParseUint(host, 10, 32); err == nil { // "0", "2130706433"
		return net.IPv4(byte(n>>24), byte(n>>16), byte(n>>8), byte(n))
	}
	return nil
}

func isDenied(ip net.IP) bool {
	return ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() ||
		ip.IsLinkLocalMulticast() || ip.IsUnspecified()
}

func verdictBlocklist(raw string) string {
	u, _ := url.Parse(raw)
	netloc := strings.ToLower(u.Host)
	host := strings.ToLower(hostOf(raw))
	for _, d := range stringDeny {
		if host == d || strings.Contains(netloc, d) {
			return "BLOCK"
		}
	}
	return "ALLOW"
}

func verdictResolvePin(raw string) (string, string) {
	ip := canonicalIP(hostOf(raw))
	if ip == nil {
		return "BLOCK", "unresolvable"
	}
	if isDenied(ip) {
		return "BLOCK", ip.String()
	}
	return "ALLOW", ip.String()
}

func main() {
	fmt.Println("Layer 7 · Topic 6 — SSRF: string blocklist vs resolve-and-pin\n")
	fmt.Printf("   %-44s%-11s%-13s%s\n", "payload", "blocklist", "resolve+pin", "resolved")
	rb, rp := 0, 0
	for _, p := range payloads {
		v1 := verdictBlocklist(p.url)
		v2, ip := verdictResolvePin(p.url)
		if v1 == "ALLOW" {
			rb++
		}
		if v2 == "ALLOW" {
			rp++
		}
		fmt.Printf("   %-44s%-11s%-13s%s\n", p.url, v1, v2, ip)
	}
	fmt.Printf("\n   internal targets reached -- string_blocklist: %d/%d   resolve_and_pin: %d/%d\n",
		rb, len(payloads), rp, len(payloads))
	fmt.Println("\nIMDS v1 vs v2: v1 returns credentials to a plain GET; v2 refuses a GET")
	fmt.Println("without a PUT-obtained token -> 0 bytes. v2 raises the bar, it does not")
	fmt.Println("fix SSRF.")
	fmt.Println("\nRead: the STRING is not the ADDRESS. DialContext hands Go the resolved")
	fmt.Println("address at connect time; validating THAT (and connecting to the pinned")
	fmt.Println("IP) closes the rebinding window a string check leaves open.")
}
