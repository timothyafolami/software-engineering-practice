// Layer 7 · Topic 3 — XSS by output context (Go / html/template).
//
// One command, no arguments: `go run xss_context.go`.
// Go's html/template is the ONE template engine here that actually solves the
// context problem: it PARSES the template as HTML, tracks which context each
// {{.}} sits in (body, attribute, href/URL, inside <script>), and picks a
// different escaper per context. Put a javascript: URL in an href and it
// emits #ZgotmplZ instead of the URL. This program renders the SAME attacker
// payload into four contexts and prints the exact bytes html/template emits,
// so you can read, per context, whether the dangerous construct survived.
//
// The determination is made from the emitted bytes, not a browser run: a cell
// is "EXECUTES" only if a live <script>, an on* handler, or a javascript:
// scheme reached the output intact. Compare to Topic 2's parameterization --
// it is the same move: make the tool understand the output language instead of
// treating it as opaque text.
//
// What to look for: every context is neutralized. html/template makes the
// context problem disappear rather than documenting it.
package main

import (
	"bytes"
	"fmt"
	"html/template"
	"strings"
)

type cell struct {
	name    string
	tmpl    string
	payload string
}

// A crude but honest "would this run in a browser?" check over emitted bytes.
func executes(html string) bool {
	low := strings.ToLower(html)
	// A construct executes only if it reached the output UNESCAPED. Each test
	// requires the literal active bytes; an entity-escaped version (&#34;,
	// &lt;) does not match, which is exactly the neutralization we are checking.
	switch {
	case strings.Contains(low, "<script>beacon()</script>"):
		return true // a live script element (body or break-out)
	case strings.Contains(low, "\" onmouseover=beacon()"):
		return true // the attribute quote survived -> handler opened
	case strings.Contains(low, "href=\"javascript:beacon()") ||
		strings.Contains(low, "href='javascript:beacon()"):
		return true // a live javascript: scheme in an href
	}
	return false
}

func main() {
	fmt.Println("Layer 7 · Topic 3 — XSS by output context (Go / html/template)\n")
	cells := []cell{
		{"body-context <script>", `<div>{{.}}</div>`, `<script>beacon()</script>`},
		{"attribute handler", `<a title="{{.}}">x</a>`, `" onmouseover=beacon() x="`},
		{"javascript: URL", `<a href="{{.}}">x</a>`, `javascript:beacon()`},
		{"</script> break-out", `<script>var c = "{{.}}";</script>`, `</script><script>beacon()</script>`},
	}
	fmt.Printf("  %-24s %-9s emitted bytes\n", "context", "executes?")
	for _, c := range cells {
		t := template.Must(template.New(c.name).Parse(c.tmpl))
		var buf bytes.Buffer
		_ = t.Execute(&buf, c.payload)
		out := buf.String()
		ex := "NO"
		if executes(out) {
			ex = "YES"
		}
		fmt.Printf("  %-24s %-9s %s\n", c.name, ex, out)
	}
	fmt.Println("\nRead: the javascript: URL becomes #ZgotmplZ, the <script> payload is\n" +
		"entity-escaped in body AND JS-string-escaped inside <script>, and the\n" +
		"attribute quote is escaped so the handler never opens. Correct-by-\n" +
		"construction, because the engine understands the output language.")
	fmt.Println("\nThe cost (README Q4): html/template can MANGLE a legitimate template\n" +
		"it cannot prove safe -- e.g. building a URL from a trusted constant plus a\n" +
		"value can emit #ZgotmplZ where you wanted a real link. Jinja never does\n" +
		"that because Jinja never looked at the context at all.")
}
