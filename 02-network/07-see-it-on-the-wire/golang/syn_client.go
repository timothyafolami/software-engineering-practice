// Layer 2 · Topic 7 - Go's contribution to the SYN table.
//
// http.DefaultTransport, untouched, and every response body read to EOF and
// closed -- which is the condition for a connection to go back to the pool at
// all. Topic 1's second Go footgun is what happens when you skip that; try
// commenting out the io.Copy below and re-running this table.
//
//	LAB_URL=http://127.0.0.1:8000/work go run syn_client.go
package main

import (
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"time"
)

func main() {
	url := os.Getenv("LAB_URL")
	if url == "" {
		url = "http://127.0.0.1:8000/work"
	}
	n, err := strconv.Atoi(os.Getenv("LAB_REQUESTS"))
	if err != nil || n <= 0 {
		n = 30
	}

	client := &http.Client{Timeout: 10 * time.Second}
	t0 := time.Now()
	for i := 0; i < n; i++ {
		resp, err := client.Get(url)
		if err != nil {
			fmt.Fprintln(os.Stderr, "request failed:", err)
			os.Exit(1)
		}
		io.Copy(io.Discard, resp.Body) // read to EOF...
		resp.Body.Close()              // ...and close. Both, or no reuse.
	}
	fmt.Printf("http.DefaultTransport, %d requests in %d ms\n", n, time.Since(t0).Milliseconds())
}
