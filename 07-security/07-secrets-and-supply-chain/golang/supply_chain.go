// Layer 7 · Topic 7 — supply chain (Go): the module system runs NO install scripts.
//
// One command: `go run supply_chain.go`. Go is the instructive contrast in this
// topic (README): the module system fetches SOURCE ONLY and runs no
// install-time hooks at all -- there is no npm postinstall, no setup.py, no
// build.rs equivalent that executes when you add a dependency. go.sum hashes
// every module, and by default the toolchain verifies against a public CHECKSUM
// DATABASE, so a republished version with different bytes is rejected by a third
// party's log, not just your local file.
//
// Go still has the DEPENDENCY problem -- the code runs when you run YOUR
// program -- it just does not have the install-EXECUTES-code problem. This file
// prints the evidence you can check yourself.
package main

import "fmt"

func main() {
	fmt.Println("Layer 7 · Topic 7 — Go's supply-chain surface")
	fmt.Println()
	fmt.Println("What runs when you `go get` / `go build` a dependency:")
	fmt.Println("   install-time scripts executed: 0  (Go has no postinstall/setup.py/build.rs)")
	fmt.Println("   integrity: every module hashed in go.sum")
	fmt.Println("   verification: GONOSUMCHECK off by default -> sum.golang.org checksum DB")
	fmt.Println()
	fmt.Println("Check for yourself:")
	fmt.Println("   go env GOFLAGS GONOSUMDB GOSUMDB   # GOSUMDB=sum.golang.org means DB checks are on")
	fmt.Println("   cat go.sum                          # one hash line per module version")
	fmt.Println()
	fmt.Println("Read: 'compiled language' buys you nothing here (see Rust's build.rs). What")
	fmt.Println("buys you something is the DESIGN: source-only fetch + a third-party checksum")
	fmt.Println("log. The code still runs when you run your program -- that risk is yours to")
	fmt.Println("manage with review and pinning, exactly like every other ecosystem.")
}
