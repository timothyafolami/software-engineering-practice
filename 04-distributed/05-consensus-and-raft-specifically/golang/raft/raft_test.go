// Layer 4 · Topic 5 — the tests. This file is what makes the harness mean
// something; without it `go test ./...` prints "no tests to run" and you have
// proved nothing at all.
//
// WHAT THESE DEMONSTRATE, part by part, in the order MIT 6.5840's Lab 3 builds
// them:
//
//	3A  leader election      one leader per term, elections after a failure,
//	                         no leader in a minority, recovery on rejoin
//	3B  log replication      agreement, agreement despite a minority failing,
//	                         no agreement without a majority, and the backup
//	                         test where a leader must overwrite a divergent tail
//	3C  persistence          a crashed-and-restarted peer must not forget its
//	                         term, its vote, or its log -- plus Figure 8, which
//	                         is the one that catches committing a previous
//	                         term's entry by replica count
//
// WHAT TO LOOK FOR: run them once and they pass. That is not the finding. Run
// `go test -race -count 20 ./...` and see whether they still do -- Raft bugs are
// overwhelmingly timing-dependent, and a run count of 1 is a coin flip.
//
//	go test -race ./...
//	go test -race -count 20 ./...
//	go test -race -run 3A -count 50 ./...
package raft

import (
	"testing"
	"time"
)

// electionTimeoutBudget is what the 6.5840 handout requires: a new leader
// within five seconds of a failure. It is deliberately far above the paper's
// 150-300ms suggestion because the handout also caps heartbeats at 10/second.
const electionTimeoutBudget = 5 * time.Second

// ------------------------------------------------------------------ 3A

func TestInitialElection3A(t *testing.T) {
	cfg := makeConfig(t, 3, true)
	defer cfg.cleanup()

	leader := cfg.checkOneLeader()
	term1 := cfg.checkTerms()
	cfg.logf("3A: initial leader is %d in term %d", leader, term1)

	// With nobody failing, the term must not keep advancing. A term that climbs
	// on an idle cluster means heartbeats are not suppressing elections, which
	// passes every liveness test and quietly triples your RPC load.
	time.Sleep(2 * time.Second)
	term2 := cfg.checkTerms()
	if term2 != term1 {
		t.Fatalf("term changed from %d to %d with no failures: heartbeats are not "+
			"suppressing elections", term1, term2)
	}
	cfg.checkOneLeader()
}

func TestReElection3A(t *testing.T) {
	cfg := makeConfig(t, 3, true)
	defer cfg.cleanup()

	leader1 := cfg.checkOneLeader()

	// Isolate the leader. The remaining two are a majority of three, so they
	// must elect a new leader -- and must do it inside the handout's budget.
	cfg.disconnect(leader1)
	start := time.Now()
	leader2 := cfg.checkOneLeader()
	elapsed := time.Since(start)
	if leader2 == leader1 {
		t.Fatalf("peer %d is still leader after being disconnected", leader1)
	}
	if elapsed > electionTimeoutBudget {
		t.Fatalf("took %v to elect a new leader, budget is %v", elapsed,
			electionTimeoutBudget)
	}
	cfg.logf("3A: re-elected %d in %v after isolating %d", leader2, elapsed, leader1)

	// The old leader rejoins. It must step down rather than fight: it will see a
	// higher term on the first RPC it exchanges. Still exactly one leader.
	cfg.connect(leader1)
	cfg.checkOneLeader()

	// Now take the majority away. Disconnect the leader and one follower,
	// leaving ONE peer connected. It cannot win an election, because a majority
	// of three is two. That is the CP choice in a single assertion.
	//
	// Note which peers get disconnected and why it has to be these. Basic Raft
	// has no leader lease and no check-quorum, so a leader that loses contact
	// with the majority does NOT step down -- it keeps believing it leads until
	// something tells it otherwise, and nothing can. Leaving the old leader
	// connected here would fail this assertion for a reason that is not a bug.
	leader3 := cfg.checkOneLeader()
	follower := (leader3 + 1) % 3
	lonely := (leader3 + 2) % 3
	cfg.disconnect(leader3)
	cfg.disconnect(follower)
	time.Sleep(2 * time.Second)
	cfg.checkNoLeader()
	cfg.logf("3A: peer %d alone cannot elect itself, as required", lonely)

	// Restore one peer: two of three is a majority again, so a leader returns.
	cfg.connect(follower)
	cfg.checkOneLeader()
}

func TestManyElections3A(t *testing.T) {
	cfg := makeConfig(t, 5, true)
	defer cfg.cleanup()

	cfg.checkOneLeader()
	for iter := 0; iter < 8; iter++ {
		// Disconnect three of five -- a minority remains, so no leader -- then
		// reconnect and require one. Randomised election timeouts are what make
		// this converge instead of splitting the vote forever.
		i1, i2, i3 := iter%5, (iter+1)%5, (iter+2)%5
		cfg.disconnect(i1)
		cfg.disconnect(i2)
		cfg.disconnect(i3)
		time.Sleep(500 * time.Millisecond)
		cfg.connect(i1)
		cfg.connect(i2)
		cfg.connect(i3)
		cfg.checkOneLeader()
	}
	cfg.checkOneLeader()
}

// ------------------------------------------------------------------ 3B

func TestBasicAgree3B(t *testing.T) {
	cfg := makeConfig(t, 3, true)
	defer cfg.cleanup()

	for index := 1; index <= 3; index++ {
		count, _ := cfg.nCommitted(index)
		if count > 0 {
			t.Fatalf("index %d was committed before anything was submitted", index)
		}
		got := cfg.one(index*100, cfg.n, false)
		if got != index {
			t.Fatalf("command landed at index %d, expected %d", got, index)
		}
	}
}

func TestFailAgree3B(t *testing.T) {
	cfg := makeConfig(t, 3, true)
	defer cfg.cleanup()

	cfg.one(101, cfg.n, false)

	// Disconnect one follower. Two of three is still a majority, so agreement
	// must continue -- with the surviving peers only.
	leader := cfg.checkOneLeader()
	follower := (leader + 1) % cfg.n
	cfg.disconnect(follower)
	cfg.one(102, cfg.n-1, false)
	cfg.one(103, cfg.n-1, false)

	// Reconnect it. It must catch up on entries it never saw, which is
	// AppendEntries walking backwards until the logs match.
	cfg.connect(follower)
	cfg.one(104, cfg.n, true)
	cfg.one(105, cfg.n, true)
	cfg.logf("3B: rejoined follower %d caught up", follower)
}

func TestFailNoAgree3B(t *testing.T) {
	cfg := makeConfig(t, 5, true)
	defer cfg.cleanup()

	cfg.one(10, cfg.n, false)

	// Take three of five away: the leader is now in a minority of two and must
	// not commit anything, however many times it is asked.
	leader := cfg.checkOneLeader()
	a, b, c := (leader+1)%5, (leader+2)%5, (leader+3)%5
	cfg.disconnect(a)
	cfg.disconnect(b)
	cfg.disconnect(c)

	index, _, ok := cfg.rafts[leader].Start(20)
	if !ok {
		t.Fatalf("peer %d stopped believing it was leader too early; it has not "+
			"heard from a higher term yet and should still accept the command",
			leader)
	}
	time.Sleep(2 * time.Second)
	if count, _ := cfg.nCommitted(index); count > 0 {
		t.Fatalf("%d peers committed index %d with only a minority connected: "+
			"the commit rule is counting the wrong thing", count, index)
	}
	cfg.logf("3B: minority leader accepted a command and correctly never committed it")

	// Heal. The majority elects a leader, and the uncommitted entry from the old
	// minority leader may or may not survive -- either is correct, and that is
	// precisely why it was never acknowledged to a client.
	cfg.connect(a)
	cfg.connect(b)
	cfg.connect(c)
	cfg.one(30, cfg.n, true)
}

func TestRejoin3B(t *testing.T) {
	cfg := makeConfig(t, 3, true)
	defer cfg.cleanup()

	cfg.one(101, cfg.n, true)

	// Isolate leader1 and let it append entries nobody else will ever see.
	leader1 := cfg.checkOneLeader()
	cfg.disconnect(leader1)
	cfg.rafts[leader1].Start(102)
	cfg.rafts[leader1].Start(103)
	cfg.rafts[leader1].Start(104)

	// The majority makes progress meanwhile.
	cfg.one(103, 2, true)

	// Bring leader1 back. Its divergent tail must be overwritten, not merged.
	cfg.connect(leader1)
	cfg.one(105, cfg.n, true)
	cfg.logf("3B: divergent tail on old leader %d was overwritten", leader1)
}

func TestBackup3B(t *testing.T) {
	cfg := makeConfig(t, 5, true)
	defer cfg.cleanup()

	cfg.one(1000, cfg.n, true)

	// Put three peers on one side of a partition and two on the other. The
	// minority side keeps a leader that appends entries which will all have to
	// be thrown away later -- 50 of them, so that a leader which walks
	// nextIndex back one entry at a time is visibly slower than one that uses
	// the conflict hint. Both are correct; only one finishes inside the budget.
	leader1 := cfg.checkOneLeader()
	minority := []int{leader1, (leader1 + 1) % 5}
	majority := []int{(leader1 + 2) % 5, (leader1 + 3) % 5, (leader1 + 4) % 5}
	cfg.net.Partition(minority, majority)
	// The minority side is still running and still talking to itself. Take it
	// out of the assertion group, not out of the network: the questions below
	// are about the majority, and an isolated leader that still believes it
	// leads is correct Raft rather than a split brain.
	for _, i := range minority {
		cfg.setConnected(i, false)
	}

	for i := 0; i < 50; i++ {
		cfg.rafts[leader1].Start(10000 + i)
	}
	time.Sleep(500 * time.Millisecond)

	// The majority side elects its own leader and commits its own entries.
	for i := 0; i < 50; i++ {
		cfg.one(20000+i, 3, true)
	}

	// Heal everything. The two peers from the minority must discard their
	// entire divergent tail and adopt the majority's log.
	cfg.net.Partition([]int{0, 1, 2, 3, 4})
	for _, i := range minority {
		cfg.setConnected(i, true)
	}
	cfg.one(30000, cfg.n, true)
	cfg.logf("3B: 50 divergent entries discarded on rejoin")
}

// ------------------------------------------------------------------ 3C

func TestPersist13C(t *testing.T) {
	cfg := makeConfig(t, 3, true)
	defer cfg.cleanup()

	cfg.one(11, cfg.n, true)

	// Crash and restart every peer, one at a time. Each must come back with its
	// log intact -- the persister is the only thing that survives, so anything
	// held in memory and not written is gone.
	for i := 0; i < cfg.n; i++ {
		cfg.crash(i)
		cfg.restart(i)
	}
	cfg.one(12, cfg.n, true)

	leader := cfg.checkOneLeader()
	cfg.crash(leader)
	cfg.restart(leader)
	cfg.one(13, cfg.n, true)
	cfg.logf("3C: state survived a crash of every peer including the leader")
}

func TestPersist23C(t *testing.T) {
	cfg := makeConfig(t, 5, true)
	defer cfg.cleanup()

	for iter := 0; iter < 3; iter++ {
		cfg.one(10+iter, cfg.n, true)

		// Crash two peers -- a minority -- restart them, and require agreement
		// across all five again. Restarting into the middle of a term is where a
		// forgotten votedFor shows up as two leaders in one term.
		leader := cfg.checkOneLeader()
		v1 := (leader + 1) % cfg.n
		v2 := (leader + 2) % cfg.n
		cfg.crash(v1)
		cfg.crash(v2)
		cfg.one(20+iter, cfg.n-2, true)
		cfg.restart(v1)
		cfg.restart(v2)
		cfg.one(30+iter, cfg.n, true)
	}
}

// TestFigure83C is the one worth reading the extended paper for.
//
// A leader may NOT commit an entry from a previous term merely because a
// majority of nodes store it. It must first commit an entry from its OWN term,
// which implicitly commits everything before it. Get that wrong and this test
// fails the way Raft bugs fail: not every run, and not the first fifty.
//
// The test does not assert on the mechanism -- it cannot, from outside. It
// hammers the cluster with crashes and restarts while committing, and relies on
// the harness checking, at every apply, that no two peers ever apply DIFFERENT
// commands at the same index. That check is the safety property; everything else
// here is just a way to give it chances to fire.
func TestFigure83C(t *testing.T) {
	cfg := makeConfig(t, 5, true)
	defer cfg.cleanup()

	cfg.one(1, 1, true)

	alive := cfg.n
	for iter := 0; iter < 100; iter++ {
		// Ask whoever thinks they are leader to append something. Most of these
		// will never commit, which is the point: they become the previous-term
		// entries that a later leader must NOT commit by counting replicas.
		for i := 0; i < cfg.n; i++ {
			if rf, ok := cfg.live(i); ok {
				if _, _, started := rf.Start(iter); started {
					break
				}
			}
		}

		time.Sleep(time.Duration(10+iter%40) * time.Millisecond)

		// Crash a random-ish live peer, keeping a majority alive so the cluster
		// can still make progress and therefore still be wrong.
		victim := (iter * 7) % cfg.n
		cfg.mu.Lock()
		down := cfg.crashed[victim]
		cfg.mu.Unlock()
		if !down && alive > cfg.n/2+1 {
			cfg.crash(victim)
			alive--
		}

		// And restart a crashed one.
		revive := (iter * 3) % cfg.n
		cfg.mu.Lock()
		down = cfg.crashed[revive]
		cfg.mu.Unlock()
		if down {
			cfg.restart(revive)
			alive++
		}
	}

	for i := 0; i < cfg.n; i++ {
		cfg.mu.Lock()
		down := cfg.crashed[i]
		cfg.mu.Unlock()
		if down {
			cfg.restart(i)
		}
	}
	cfg.one(9999, cfg.n, true)
	cfg.logf("3C: survived 100 rounds of crash-while-committing with no divergence")
}

// TestUnreliableAgree3B runs agreement over a network that drops and delays
// RPCs. Everything above assumes messages arrive; this one does not, and it is
// the cheapest way to find code that treats "no reply" as "no".
func TestUnreliableAgree3B(t *testing.T) {
	cfg := makeConfig(t, 5, false)
	defer cfg.cleanup()

	for i := 1; i <= 12; i++ {
		cfg.one(i, cfg.n-1, true)
	}
	cfg.net.SetReliable(true)
	cfg.one(9000, cfg.n, true)
}
