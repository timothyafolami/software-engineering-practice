// Layer 1 - Blocking vs non-blocking IO.
// net.Conn.Read/Write present a blocking-looking API to your goroutine --
// the call appears to just wait until data is ready, the way a classic
// blocking socket would. Underneath, Go's runtime registers the file
// descriptor with its netpoller (epoll on Linux) and parks ONLY the
// goroutine, not an OS thread, until the fd is ready -- then resumes it.
// That's why "serial" below still doesn't tie up a full OS thread per
// waiting request the way a truly blocking read(2) call in C would; it's
// a middle ground between Python's raw blocking sockets and Node's
// explicit event-driven API, and it's a big part of why Go code reads like
// synchronous code but scales like async code.
package main

import (
	"fmt"
	"net"
	"sync"
	"time"
)

const ResponseDelay = 100 * time.Millisecond
const N = 20

func startServer() net.Listener {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		panic(err)
	}
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go handleConn(conn)
		}
	}()
	return ln
}

func handleConn(conn net.Conn) {
	defer conn.Close()
	buf := make([]byte, 1024)
	conn.Read(buf)
	time.Sleep(ResponseDelay)
	conn.Write([]byte("ok"))
}

func doRequest(addr string) {
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		panic(err)
	}
	defer conn.Close()
	conn.Write([]byte("ping"))
	buf := make([]byte, 1024)
	conn.Read(buf)
}

func benchSerial(addr string) time.Duration {
	start := time.Now()
	for i := 0; i < N; i++ {
		doRequest(addr)
	}
	return time.Since(start)
}

func benchConcurrent(addr string) time.Duration {
	start := time.Now()
	var wg sync.WaitGroup
	for i := 0; i < N; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			doRequest(addr)
		}()
	}
	wg.Wait()
	return time.Since(start)
}

func main() {
	ln := startServer()
	defer ln.Close()
	addr := ln.Addr().String()

	tSerial := benchSerial(addr)
	tConcurrent := benchConcurrent(addr)
	fmt.Printf("N=%d requests, %v server delay each\n", N, ResponseDelay)
	fmt.Printf("serial (net.Dial, one goroutine):   %v  (~%v/req)\n", tSerial, tSerial/N)
	fmt.Printf("concurrent (goroutine per request):  %v\n", tConcurrent)
}
