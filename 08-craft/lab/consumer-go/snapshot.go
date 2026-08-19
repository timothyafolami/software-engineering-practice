package main

import (
	"os"
	"path/filepath"
)

// readSnapshot locates the committed contract relative to this module.
// Deliberately not embedded: the point of topic 6 is that the contract is a
// FILE that outlives both sides, and embedding a copy would create a second one.
func readSnapshot() ([]byte, error) {
	candidates := []string{
		filepath.Join("..", "api", "openapi.snapshot.json"),
		filepath.Join("api", "openapi.snapshot.json"),
		"/work/api/openapi.snapshot.json",
	}
	var lastErr error
	for _, c := range candidates {
		b, err := os.ReadFile(c)
		if err == nil {
			return b, nil
		}
		lastErr = err
	}
	return nil, lastErr
}
