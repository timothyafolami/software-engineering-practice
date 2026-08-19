// Layer 1 - Same experiment, from Go's side. syscall.Getrlimit gives us
// the ceiling directly (no /proc parsing needed on the languages that
// expose it in their standard library).
package main

import (
	"fmt"
	"os"
	"syscall"
)

func main() {
	var rlimit syscall.Rlimit
	if err := syscall.Getrlimit(syscall.RLIMIT_NOFILE, &rlimit); err != nil {
		panic(err)
	}
	fmt.Printf("RLIMIT_NOFILE: soft=%d, hard=%d\n", rlimit.Cur, rlimit.Max)

	var fds []*os.File
	for {
		f, err := os.Open("/dev/null")
		if err != nil {
			fmt.Printf("hit error ('too many open files') after opening %d fds: %v\n", len(fds), err)
			break
		}
		fds = append(fds, f)
	}
	for _, f := range fds {
		f.Close()
	}
	fmt.Printf("closed all %d fds; process is healthy again\n", len(fds))
}
