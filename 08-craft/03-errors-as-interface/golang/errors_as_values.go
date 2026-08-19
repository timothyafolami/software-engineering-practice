// Layer 8 Topic 3 - Go: sentinels, error types, %w, and the wrapping that says nothing.
//
// WHAT THIS DEMONSTRATES: Go's real advantage is not verbosity, it is that
// `if err != nil` makes "what does the caller do about this" a question you
// cannot skip syntactically. The categories live in the type system by
// convention: sentinel + errors.Is for caller-actionable, a custom type +
// errors.As when the caller needs DATA out of the error, panic for bugs.
//
// And Go's own failure mode, which is the opposite of Python's: an error wrapped
// and returned at every level with no added information produces a message that
// names four layers and no cause.
//
// WHAT TO LOOK FOR: the two message strings printed side by side. The BROKEN one
// is four layer names and a verb. The FIXED one names the operation, the
// identifier and the underlying condition -- and `errors.As` can still pull the
// retry-after value out of it, which a string never could.
//
//	cd golang && go run errors_as_values.go
package main

import (
	"errors"
	"fmt"
	"time"
)

// Category 1: caller-actionable, tested with errors.Is. A sentinel, because the
// caller needs to branch on it and needs nothing out of it.
var ErrOrderNotFound = errors.New("order not found")

// Category 2: retryable, and the caller needs DATA out of it (how long to wait),
// so it is a type tested with errors.As rather than a sentinel.
type UnavailableError struct {
	Dep        string
	RetryAfter time.Duration
	Err        error
}

func (e *UnavailableError) Error() string {
	return fmt.Sprintf("%s unavailable (retry after %s): %v", e.Dep, e.RetryAfter, e.Err)
}
func (e *UnavailableError) Unwrap() error { return e.Err }

// --- BROKEN: wrapped at every level, information added at none of them -------

func daoBroken() error {
	return &UnavailableError{Dep: "postgres", RetryAfter: 2 * time.Second, Err: errors.New("dial tcp 10.0.0.7:5432: connect: connection refused")}
}
func repoBroken() error {
	if err := daoBroken(); err != nil {
		return fmt.Errorf("repository: %w", err)
	}
	return nil
}
func svcBroken() error {
	if err := repoBroken(); err != nil {
		return fmt.Errorf("service: %w", err)
	}
	return nil
}
func httpBroken() error {
	if err := svcBroken(); err != nil {
		return fmt.Errorf("handler: %w", err)
	}
	return nil
}

// --- FIXED: wrap ONCE, at the boundary, with the thing the caller lacks -------
//
// The rule that survives contact with a codebase: add context only where you
// have context the callee did not. A layer that knows nothing the callee did not
// know should return the error unchanged.

func daoFixed(orderID int64) error {
	return &UnavailableError{
		Dep:        "postgres",
		RetryAfter: 2 * time.Second,
		Err:        errors.New("dial tcp 10.0.0.7:5432: connect: connection refused"),
	}
}

func repoFixed(orderID int64) error {
	// This layer knows the order id. The dao does not. That is context worth adding.
	if err := daoFixed(orderID); err != nil {
		return fmt.Errorf("load order %d: %w", orderID, err)
	}
	return nil
}

func svcFixed(orderID int64) error {
	// This layer knows nothing the repository did not. So it adds nothing.
	return repoFixed(orderID)
}

func main() {
	fmt.Println("=== BROKEN: four layer names, no new information ===")
	fmt.Printf("  %v\n", httpBroken())
	fmt.Println("  -> 'handler: service: repository:' tells you the call stack, which")
	fmt.Println("     you already had, and nothing about what to do next.")

	fmt.Println("\n=== FIXED: wrapped once, where there was something to add ===")
	err := svcFixed(4711)
	fmt.Printf("  %v\n", err)

	fmt.Println("\n=== the categories, tested rather than string-matched ===")
	var unavailable *UnavailableError
	if errors.As(err, &unavailable) {
		fmt.Printf("  errors.As  -> category 2: retry %s against %s\n",
			unavailable.RetryAfter, unavailable.Dep)
		fmt.Println("                 the Retry-After came OUT of the error as a value.")
		fmt.Println("                 No string parsing, and it survives every wrap above it.")
	}
	if errors.Is(err, ErrOrderNotFound) {
		fmt.Println("  errors.Is  -> category 1: 404")
	} else {
		fmt.Println("  errors.Is  -> not ErrOrderNotFound, so this is NOT a 404")
	}

	fmt.Println("\n=== category 3: the one that must not be an error value ===")
	fmt.Printf("  %v\n", recovered(func() { var m map[string]int; m["boom"] = 1 }))
	fmt.Println("  -> a nil-map write is a bug. Returning it as an `error` invites a")
	fmt.Println("     caller to handle it, and there is nothing to handle. In a server")
	fmt.Println("     you recover at the request boundary ONLY to turn it into a 500 and")
	fmt.Println("     a log line -- never to continue as though it did not happen.")
}

func recovered(f func()) (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("panic (category 3, bug): %v", r)
		}
	}()
	f()
	return nil
}
