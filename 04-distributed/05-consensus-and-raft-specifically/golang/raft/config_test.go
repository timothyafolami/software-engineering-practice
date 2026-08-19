// Layer 4 · Topic 5 — the test harness.
//
// This is the part that decides whether the tests mean anything. Two rules it
// enforces that a naive harness does not:
//
//  1. checkOneLeader retries. A cluster that is mid-election has zero leaders,
//     and asserting on the first sample would make every test flaky in a way
//     that looks like a Raft bug. It samples repeatedly and only fails if the
//     cluster cannot settle -- but it fails immediately and loudly on TWO
//     leaders in the same term, which is a genuine safety violation and must
//     never be retried away.
//
//  2. Every applied entry is checked for agreement as it arrives, not at the
//     end. If two peers apply different commands at the same index, that is
//     caught at the moment it happens, with both values, rather than as a
//     mysterious count mismatch later.
//
//  3. Assertions are scoped to the peers the test currently CARES about, tracked
//     in `connected`. This matters more than it looks: an isolated leader keeps
//     believing it is the leader, forever, because nothing can tell it
//     otherwise -- that is correct Raft, not a bug. A checkOneLeader that
//     counted it would either report a split brain that does not exist or hand
//     back the deposed leader. Both make the test lie.
package raft

import (
	"fmt"
	"sync"
	"testing"
	"time"
)

type config struct {
	t   *testing.T
	mu  sync.Mutex
	n   int
	net *Network

	rafts      []*Raft
	persisters []*Persister
	applyChs   []chan ApplyMsg
	crashed    []bool
	// connected[i] is whether peer i is part of the group this test is making
	// assertions about. Disconnect/Connect keep it in step with the network;
	// setConnected exists for partition tests, where the network reality and the
	// group under assertion are deliberately different things.
	connected []bool

	// logs[i][index] is what peer i applied at that log index.
	logs []map[int]interface{}

	start time.Time
}

func makeConfig(t *testing.T, n int, reliable bool) *config {
	cfg := &config{
		t:          t,
		n:          n,
		net:        NewNetwork(n),
		rafts:      make([]*Raft, n),
		persisters: make([]*Persister, n),
		applyChs:   make([]chan ApplyMsg, n),
		crashed:    make([]bool, n),
		connected:  make([]bool, n),
		logs:       make([]map[int]interface{}, n),
		start:      time.Now(),
	}
	cfg.net.SetReliable(reliable)
	for i := 0; i < n; i++ {
		cfg.persisters[i] = NewPersister()
		cfg.logs[i] = map[int]interface{}{}
		cfg.connected[i] = true
		cfg.startPeer(i)
	}
	return cfg
}

func (cfg *config) startPeer(i int) {
	ch := make(chan ApplyMsg, 1024)
	cfg.applyChs[i] = ch
	rf := Make(cfg.net, i, cfg.persisters[i], ch)
	cfg.mu.Lock()
	cfg.rafts[i] = rf
	cfg.crashed[i] = false
	cfg.mu.Unlock()
	go cfg.collect(i, rf, ch)
}

// collect records what peer i applies and checks agreement on the spot.
func (cfg *config) collect(i int, rf *Raft, ch chan ApplyMsg) {
	for msg := range ch {
		if !msg.CommandValid {
			continue
		}
		cfg.mu.Lock()
		for other := 0; other < cfg.n; other++ {
			if seen, ok := cfg.logs[other][msg.CommandIndex]; ok && seen != msg.Command {
				cfg.mu.Unlock()
				// Not retried, not tolerated: two peers applied different
				// commands at the same index. Every other bug in Raft is a
				// liveness problem; this one is a safety violation.
				cfg.t.Errorf("APPLY DISAGREEMENT at index %d: peer %d applied %v, peer %d applied %v",
					msg.CommandIndex, i, msg.Command, other, seen)
				return
			}
		}
		cfg.logs[i][msg.CommandIndex] = msg.Command
		cfg.mu.Unlock()
	}
}

// crash stops a peer but keeps its persister, which is what makes a restart a
// restart rather than a fresh node.
// disconnect isolates peer i on the network AND removes it from the group this
// test asserts about. Always use this rather than cfg.net.Disconnect directly.
func (cfg *config) disconnect(i int) {
	cfg.net.Disconnect(i)
	cfg.mu.Lock()
	cfg.connected[i] = false
	cfg.mu.Unlock()
}

func (cfg *config) connect(i int) {
	cfg.net.Connect(i)
	cfg.mu.Lock()
	cfg.connected[i] = true
	cfg.mu.Unlock()
}

// setConnected changes only the assertion group, not the network. Used by
// partition tests, where a minority side is genuinely still running and talking
// to itself, and the test's questions are about the majority side.
func (cfg *config) setConnected(i int, v bool) {
	cfg.mu.Lock()
	cfg.connected[i] = v
	cfg.mu.Unlock()
}

// live reports whether peer i is running and in the assertion group.
func (cfg *config) live(i int) (*Raft, bool) {
	cfg.mu.Lock()
	defer cfg.mu.Unlock()
	if cfg.crashed[i] || !cfg.connected[i] || cfg.rafts[i] == nil {
		return nil, false
	}
	return cfg.rafts[i], true
}

func (cfg *config) crash(i int) {
	cfg.mu.Lock()
	rf := cfg.rafts[i]
	cfg.crashed[i] = true
	cfg.connected[i] = false
	cfg.mu.Unlock()

	cfg.net.setDown(i, true)
	if rf != nil {
		rf.Kill()
		rf.wake()
	}
}

func (cfg *config) restart(i int) {
	cfg.net.setDown(i, false)
	cfg.mu.Lock()
	cfg.connected[i] = true
	cfg.mu.Unlock()
	cfg.startPeer(i)
}

func (cfg *config) cleanup() {
	for i := 0; i < cfg.n; i++ {
		cfg.mu.Lock()
		rf := cfg.rafts[i]
		cfg.mu.Unlock()
		if rf != nil {
			rf.Kill()
			rf.wake()
		}
	}
}

// checkOneLeader returns the index of the single leader, retrying while the
// cluster settles. Two leaders in the SAME term fails immediately.
func (cfg *config) checkOneLeader() int {
	cfg.t.Helper()
	for attempt := 0; attempt < 12; attempt++ {
		time.Sleep(250 * time.Millisecond)

		leadersByTerm := map[int][]int{}
		for i := 0; i < cfg.n; i++ {
			rf, ok := cfg.live(i)
			if !ok {
				continue
			}
			if term, isLeader := rf.GetState(); isLeader {
				leadersByTerm[term] = append(leadersByTerm[term], i)
			}
		}
		latest := -1
		for term, leaders := range leadersByTerm {
			if len(leaders) > 1 {
				cfg.t.Fatalf("SPLIT BRAIN: term %d has %d leaders: %v", term, len(leaders), leaders)
			}
			if term > latest {
				latest = term
			}
		}
		if latest != -1 {
			return leadersByTerm[latest][0]
		}
	}
	cfg.t.Fatalf("no leader elected within %v", 12*250*time.Millisecond)
	return -1
}

func (cfg *config) checkNoLeader() {
	cfg.t.Helper()
	for i := 0; i < cfg.n; i++ {
		rf, ok := cfg.live(i)
		if !ok {
			continue
		}
		if term, isLeader := rf.GetState(); isLeader {
			cfg.t.Fatalf("peer %d claims leadership in term %d while it cannot reach a majority",
				i, term)
		}
	}
}

// checkTerms returns the term every reachable peer agrees on, or -1 if they
// disagree (which is normal mid-election).
func (cfg *config) checkTerms() int {
	term := -1
	for i := 0; i < cfg.n; i++ {
		rf, ok := cfg.live(i)
		if !ok {
			continue
		}
		t, _ := rf.GetState()
		if term == -1 {
			term = t
		} else if term != t {
			return -1
		}
	}
	return term
}

// nCommitted reports how many peers have applied something at this index, and
// what it was.
func (cfg *config) nCommitted(index int) (int, interface{}) {
	count := 0
	var command interface{}
	cfg.mu.Lock()
	defer cfg.mu.Unlock()
	for i := 0; i < cfg.n; i++ {
		if v, ok := cfg.logs[i][index]; ok {
			command = v
			count++
		}
	}
	return count, command
}

// one submits a command and waits for `expected` peers to apply it at the same
// index. Retries against a different peer if the first was not the leader,
// because "who is the leader" is exactly the thing that is not knowable
// synchronously.
func (cfg *config) one(command interface{}, expected int, retry bool) int {
	cfg.t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	starter := 0
	for time.Now().Before(deadline) {
		index := -1
		for range cfg.n {
			cfg.mu.Lock()
			rf, down := cfg.rafts[starter%cfg.n], cfg.crashed[starter%cfg.n]
			cfg.mu.Unlock()
			starter++
			if down || rf == nil {
				continue
			}
			if idx, _, ok := rf.Start(command); ok {
				index = idx
				break
			}
		}

		if index != -1 {
			until := time.Now().Add(2 * time.Second)
			for time.Now().Before(until) {
				count, applied := cfg.nCommitted(index)
				if count >= expected && applied == command {
					return index
				}
				time.Sleep(20 * time.Millisecond)
			}
			if !retry {
				cfg.t.Fatalf("command %v never committed to %d peers at index %d",
					command, expected, index)
			}
		} else {
			time.Sleep(50 * time.Millisecond)
		}
	}
	cfg.t.Fatalf("command %v never committed by any leader", command)
	return -1
}

func (cfg *config) logf(format string, args ...interface{}) {
	cfg.t.Logf("  %6.2fs  %s", time.Since(cfg.start).Seconds(), fmt.Sprintf(format, args...))
}
