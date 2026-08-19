// Layer 4 · Topic 5 — Raft: leader election, log replication, persistence.
//
// WHAT THIS IS: a working Raft that passes a test suite, covering the ground of
// MIT 6.5840 Lab 3 parts 3A (election), 3B (replication) and 3C (persistence).
// Snapshots (3D) are deliberately absent -- see the topic README for why, and
// for how to get the real thing.
//
// It follows the extended paper's Figure 2 literally. Where the code departs
// from a naive reading of Figure 2 there is a comment saying why, because those
// are the places implementations are silently wrong:
//
//   - commitIndex only ever advances onto an entry from the LEADER'S OWN TERM
//     (advanceCommitIndex). This is Figure 8, and it is the rule that a
//     replica-counting implementation gets wrong while passing every simple
//     test. There is a dedicated deterministic test for it.
//   - The election restriction (§5.4.1) is in RequestVote: a candidate whose log
//     is behind the voter's does not get the vote, whatever its term.
//   - Persistence happens BEFORE the reply is sent, never after. A vote granted
//     but not yet durable is how you get two leaders in one term.
//   - An AppendEntries whose entries are already present must NOT truncate the
//     follower's log. Truncating on every call deletes committed entries when a
//     stale heartbeat arrives out of order, and it is the single most common
//     3B bug.
//
// Concurrency rule used throughout: no RPC is ever issued while holding rf.mu,
// and no channel send happens while holding rf.mu. Both are deadlocks that only
// appear under load.
package raft

import (
	"math/rand"
	"sync"
	"sync/atomic"
	"time"
)

// Timing. The paper suggests 150-300ms election timeouts; MIT's harness caps
// heartbeats at 10/sec and wants a new leader inside 5s, so the timeouts here
// are wider than the paper's. Randomised range is deliberately more than 2x the
// heartbeat interval, or a slow heartbeat becomes an election.
const (
	heartbeatInterval  = 100 * time.Millisecond
	electionTimeoutMin = 400 * time.Millisecond
	electionTimeoutMax = 800 * time.Millisecond
	tickInterval       = 15 * time.Millisecond
)

type Role int

const (
	Follower Role = iota
	Candidate
	Leader
)

func (r Role) String() string {
	switch r {
	case Follower:
		return "follower"
	case Candidate:
		return "candidate"
	default:
		return "leader"
	}
}

// ApplyMsg is what a Raft peer hands to the state machine above it. Entries are
// delivered in index order, exactly once, and only after they are committed.
type ApplyMsg struct {
	CommandValid bool
	Command      interface{}
	CommandIndex int
}

type LogEntry struct {
	Term    int
	Command interface{}
}

type Raft struct {
	mu        sync.Mutex
	net       *Network
	me        int
	persister *Persister
	applyCh   chan ApplyMsg
	applyCond *sync.Cond
	dead      int32

	// --- persistent state (Figure 2). Must reach stable storage before any
	// reply that depends on it.
	currentTerm int
	votedFor    int        // -1 for none
	log         []LogEntry // log[0] is a sentinel {Term: 0}; real entries start at 1

	// --- volatile state on all servers
	commitIndex int
	lastApplied int

	// --- volatile state on leaders, reinitialised after every election
	nextIndex  []int
	matchIndex []int

	role          Role
	lastHeard     time.Time
	electionAfter time.Duration
	lastBroadcast time.Time
}

func Make(net *Network, me int, persister *Persister, applyCh chan ApplyMsg) *Raft {
	rf := &Raft{
		net:         net,
		me:          me,
		persister:   persister,
		applyCh:     applyCh,
		currentTerm: 0,
		votedFor:    -1,
		log:         []LogEntry{{Term: 0}},
		role:        Follower,
	}
	rf.applyCond = sync.NewCond(&rf.mu)
	rf.nextIndex = make([]int, net.size())
	rf.matchIndex = make([]int, net.size())

	// Restart path: whatever survived the crash is the truth, and anything not
	// on this list must be rebuilt from the leader rather than remembered.
	if state, ok := decodeState(persister.Read()); ok {
		rf.currentTerm = state.CurrentTerm
		rf.votedFor = state.VotedFor
		rf.log = state.Log
	}

	rf.resetElectionTimer()
	net.attach(me, rf)

	go rf.ticker()
	go rf.applier()
	return rf
}

// --- small helpers, all assuming rf.mu is held --------------------------------

func (rf *Raft) lastLogIndex() int { return len(rf.log) - 1 }
func (rf *Raft) lastLogTerm() int  { return rf.log[len(rf.log)-1].Term }

func (rf *Raft) persist() {
	rf.persister.Save(encodeState(persistentState{
		CurrentTerm: rf.currentTerm,
		VotedFor:    rf.votedFor,
		Log:         rf.log,
	}))
}

func (rf *Raft) resetElectionTimer() {
	rf.lastHeard = time.Now()
	span := int(electionTimeoutMax - electionTimeoutMin)
	rf.electionAfter = electionTimeoutMin + time.Duration(rand.Intn(span))
}

// becomeFollower is the "if RPC request or response contains term T >
// currentTerm" rule from Figure 2, in one place so it cannot be forgotten in
// one of the four call sites.
func (rf *Raft) becomeFollower(term int) {
	rf.currentTerm = term
	rf.votedFor = -1
	rf.role = Follower
	rf.persist()
}

func (rf *Raft) majority() int { return rf.net.size()/2 + 1 }

// --- public surface -----------------------------------------------------------

func (rf *Raft) GetState() (int, bool) {
	rf.mu.Lock()
	defer rf.mu.Unlock()
	return rf.currentTerm, rf.role == Leader
}

// Start asks this peer to append a command to the replicated log. It returns
// immediately; the entry is committed later, or never if this peer turns out not
// to be the leader. There is no way for a caller to be told "yes" synchronously
// without a round trip to a majority, which is the cost of consensus.
func (rf *Raft) Start(command interface{}) (int, int, bool) {
	rf.mu.Lock()
	if rf.role != Leader || rf.killed() {
		term := rf.currentTerm
		rf.mu.Unlock()
		return -1, term, false
	}
	rf.log = append(rf.log, LogEntry{Term: rf.currentTerm, Command: command})
	index := rf.lastLogIndex()
	term := rf.currentTerm
	rf.persist()
	rf.mu.Unlock()

	rf.broadcastAppendEntries()
	return index, term, true
}

func (rf *Raft) Kill()        { atomic.StoreInt32(&rf.dead, 1) }
func (rf *Raft) killed() bool { return atomic.LoadInt32(&rf.dead) == 1 }

// --- RPC: RequestVote ----------------------------------------------------------

type RequestVoteArgs struct {
	Term         int
	CandidateID  int
	LastLogIndex int
	LastLogTerm  int
}

type RequestVoteReply struct {
	Term        int
	VoteGranted bool
}

func (rf *Raft) RequestVote(args *RequestVoteArgs, reply *RequestVoteReply) {
	rf.mu.Lock()
	defer rf.mu.Unlock()

	reply.VoteGranted = false
	if args.Term < rf.currentTerm {
		reply.Term = rf.currentTerm
		return
	}
	if args.Term > rf.currentTerm {
		rf.becomeFollower(args.Term)
	}
	reply.Term = rf.currentTerm

	// The election restriction, §5.4.1. This is what guarantees a new leader
	// already holds every committed entry, and it is why Raft needs no separate
	// "catch the leader up" phase. Get this wrong and committed entries vanish.
	upToDate := args.LastLogTerm > rf.lastLogTerm() ||
		(args.LastLogTerm == rf.lastLogTerm() && args.LastLogIndex >= rf.lastLogIndex())

	if (rf.votedFor == -1 || rf.votedFor == args.CandidateID) && upToDate {
		rf.votedFor = args.CandidateID
		rf.persist() // durable BEFORE the reply leaves. See persister.go.
		// Only a granted vote resets the timer. Resetting it on every
		// RequestVote lets a node with a stale log suppress elections forever.
		rf.resetElectionTimer()
		reply.VoteGranted = true
	}
}

// --- RPC: AppendEntries ---------------------------------------------------------

type AppendEntriesArgs struct {
	Term         int
	LeaderID     int
	PrevLogIndex int
	PrevLogTerm  int
	Entries      []LogEntry
	LeaderCommit int
}

type AppendEntriesReply struct {
	Term    int
	Success bool
	// Fast backup (paper §5.3, "an optimization"). Without it a leader walks
	// nextIndex back one entry per round trip, which turns a 1000-entry
	// divergence into 1000 heartbeat intervals and blows every timing budget.
	ConflictTerm  int
	ConflictIndex int
}

func (rf *Raft) AppendEntries(args *AppendEntriesArgs, reply *AppendEntriesReply) {
	rf.mu.Lock()
	defer rf.mu.Unlock()

	reply.Success = false
	reply.ConflictTerm = -1
	reply.ConflictIndex = -1

	if args.Term < rf.currentTerm {
		reply.Term = rf.currentTerm
		return
	}
	if args.Term > rf.currentTerm {
		rf.becomeFollower(args.Term)
	}
	// A candidate that hears from a leader of the current term steps down. So
	// does a leader, which cannot happen if the invariants hold -- but relying
	// on that is how you find out they do not.
	rf.role = Follower
	rf.resetElectionTimer()
	reply.Term = rf.currentTerm

	// Consistency check. If we do not have the entry the leader thinks precedes
	// its batch, reject and tell it where to resume.
	if args.PrevLogIndex > rf.lastLogIndex() {
		reply.ConflictIndex = rf.lastLogIndex() + 1
		reply.ConflictTerm = -1
		return
	}
	if rf.log[args.PrevLogIndex].Term != args.PrevLogTerm {
		reply.ConflictTerm = rf.log[args.PrevLogIndex].Term
		i := args.PrevLogIndex
		for i > 1 && rf.log[i-1].Term == reply.ConflictTerm {
			i--
		}
		reply.ConflictIndex = i
		return
	}

	// Append, but only truncate where the logs actually disagree.
	//
	// THE 3B BUG: `rf.log = append(rf.log[:args.PrevLogIndex+1], args.Entries...)`
	// looks equivalent and is not. RPCs can be reordered by the network, so an
	// older, shorter AppendEntries can arrive after a newer one; the naive line
	// then deletes entries this follower has already had committed, and the test
	// failure surfaces somewhere else entirely.
	for i, entry := range args.Entries {
		idx := args.PrevLogIndex + 1 + i
		if idx <= rf.lastLogIndex() {
			if rf.log[idx].Term == entry.Term {
				continue // already have it, identical
			}
			rf.log = rf.log[:idx] // genuine conflict: everything after goes
		}
		rf.log = append(rf.log, args.Entries[i:]...)
		break
	}
	rf.persist()

	if args.LeaderCommit > rf.commitIndex {
		// min(leaderCommit, index of last new entry). Not min(leaderCommit,
		// lastLogIndex): if this follower is ahead because of a stale message,
		// the leader's commit index says nothing about those extra entries.
		lastNew := args.PrevLogIndex + len(args.Entries)
		rf.commitIndex = min(args.LeaderCommit, lastNew)
		rf.applyCond.Signal()
	}
	reply.Success = true
}

// --- the timing loop ------------------------------------------------------------

func (rf *Raft) ticker() {
	for !rf.killed() {
		time.Sleep(tickInterval)

		rf.mu.Lock()
		switch rf.role {
		case Leader:
			if time.Since(rf.lastBroadcast) >= heartbeatInterval {
				rf.lastBroadcast = time.Now()
				rf.mu.Unlock()
				rf.broadcastAppendEntries()
				continue
			}
		default:
			if time.Since(rf.lastHeard) >= rf.electionAfter {
				rf.mu.Unlock()
				rf.startElection()
				continue
			}
		}
		rf.mu.Unlock()
	}
}

func (rf *Raft) startElection() {
	rf.mu.Lock()
	if rf.role == Leader || rf.killed() {
		rf.mu.Unlock()
		return
	}
	rf.currentTerm++
	rf.role = Candidate
	rf.votedFor = rf.me
	rf.persist()
	rf.resetElectionTimer()

	args := RequestVoteArgs{
		Term:         rf.currentTerm,
		CandidateID:  rf.me,
		LastLogIndex: rf.lastLogIndex(),
		LastLogTerm:  rf.lastLogTerm(),
	}
	term := rf.currentTerm
	rf.mu.Unlock()

	var votes int32 = 1 // itself
	for peer := 0; peer < rf.net.size(); peer++ {
		if peer == rf.me {
			continue
		}
		go func(peer int) {
			var reply RequestVoteReply
			if !rf.net.Call(rf.me, peer, "Raft.RequestVote", &args, &reply) {
				return // lost request, lost reply, or dead peer: indistinguishable
			}
			rf.mu.Lock()
			defer rf.mu.Unlock()

			if reply.Term > rf.currentTerm {
				rf.becomeFollower(reply.Term)
				return
			}
			// Stale reply from an election we have already left. Acting on it
			// would let a node become leader for a term it is no longer in.
			if rf.role != Candidate || rf.currentTerm != term {
				return
			}
			if reply.VoteGranted && int(atomic.AddInt32(&votes, 1)) >= rf.majority() {
				rf.becomeLeaderLocked()
			}
		}(peer)
	}
}

// becomeLeaderLocked assumes rf.mu is held.
func (rf *Raft) becomeLeaderLocked() {
	if rf.role != Candidate {
		return // already won this election, or already stepped down
	}
	rf.role = Leader
	for i := range rf.nextIndex {
		// Optimistic: assume every follower matches, and let the consistency
		// check walk it back. The alternative (start at 0) is correct but costs
		// a full log replay to every follower on every election.
		rf.nextIndex[i] = rf.lastLogIndex() + 1
		rf.matchIndex[i] = 0
	}
	rf.matchIndex[rf.me] = rf.lastLogIndex()
	rf.lastBroadcast = time.Time{} // force an immediate heartbeat
	go rf.broadcastAppendEntries()
}

func (rf *Raft) broadcastAppendEntries() {
	rf.mu.Lock()
	if rf.role != Leader {
		rf.mu.Unlock()
		return
	}
	term := rf.currentTerm
	rf.lastBroadcast = time.Now()
	rf.mu.Unlock()

	for peer := 0; peer < rf.net.size(); peer++ {
		if peer == rf.me {
			continue
		}
		go rf.replicateTo(peer, term)
	}
}

func (rf *Raft) replicateTo(peer, term int) {
	rf.mu.Lock()
	if rf.role != Leader || rf.currentTerm != term {
		rf.mu.Unlock()
		return
	}
	next := rf.nextIndex[peer]
	if next < 1 {
		next = 1
	}
	if next > rf.lastLogIndex()+1 {
		next = rf.lastLogIndex() + 1
	}
	args := AppendEntriesArgs{
		Term:         term,
		LeaderID:     rf.me,
		PrevLogIndex: next - 1,
		PrevLogTerm:  rf.log[next-1].Term,
		Entries:      append([]LogEntry(nil), rf.log[next:]...),
		LeaderCommit: rf.commitIndex,
	}
	rf.mu.Unlock()

	var reply AppendEntriesReply
	if !rf.net.Call(rf.me, peer, "Raft.AppendEntries", &args, &reply) {
		return
	}

	rf.mu.Lock()
	defer rf.mu.Unlock()

	if reply.Term > rf.currentTerm {
		rf.becomeFollower(reply.Term)
		return
	}
	// Same staleness guard as in the election. A reply that arrives after we
	// stopped being leader for this term tells us nothing about now.
	if rf.role != Leader || rf.currentTerm != term {
		return
	}

	if reply.Success {
		match := args.PrevLogIndex + len(args.Entries)
		// Never move matchIndex backwards: an out-of-order reply from an older,
		// shorter batch would otherwise un-commit entries.
		if match > rf.matchIndex[peer] {
			rf.matchIndex[peer] = match
			rf.nextIndex[peer] = match + 1
		}
		rf.advanceCommitIndex()
		return
	}

	// Fast backup: jump over the whole conflicting term rather than one entry.
	if reply.ConflictTerm == -1 {
		rf.nextIndex[peer] = max(1, reply.ConflictIndex)
		return
	}
	last := -1
	for i := rf.lastLogIndex(); i >= 1; i-- {
		if rf.log[i].Term == reply.ConflictTerm {
			last = i
			break
		}
	}
	if last >= 0 {
		rf.nextIndex[peer] = last + 1
	} else {
		rf.nextIndex[peer] = max(1, reply.ConflictIndex)
	}
}

// advanceCommitIndex is Figure 8, and it is the reason this file exists.
//
// The tempting rule is "if a majority stores entry N, N is committed". It is
// wrong, and the counterexample is Figure 8 in the extended paper: a leader can
// replicate a previous-term entry to a majority, crash, and a later leader with
// a different log can still be elected and overwrite it -- so an entry that was
// "committed" by replica count gets removed, which breaks the state machine
// safety property outright.
//
// The fix is one clause: only ever advance the commit index onto an entry from
// the CURRENT term. Everything before it is committed implicitly by the Log
// Matching Property. See figure8/main.go for the sequence spelled out, and
// TestNoCommitOfPreviousTermByCount for the assertion.
//
// Assumes rf.mu is held.
func (rf *Raft) advanceCommitIndex() {
	for n := rf.lastLogIndex(); n > rf.commitIndex; n-- {
		if rf.log[n].Term != rf.currentTerm {
			continue // <- the entire Figure 8 fix
		}
		replicas := 1 // the leader itself
		for peer := 0; peer < rf.net.size(); peer++ {
			if peer != rf.me && rf.matchIndex[peer] >= n {
				replicas++
			}
		}
		if replicas >= rf.majority() {
			rf.commitIndex = n
			rf.applyCond.Signal()
			return
		}
	}
}

// applier delivers committed entries to the state machine, in index order,
// exactly once. It sends on applyCh with rf.mu released: holding a mutex across
// a channel send to code you do not control is a deadlock waiting for the day
// that code calls back into you.
func (rf *Raft) applier() {
	for !rf.killed() {
		rf.mu.Lock()
		for rf.lastApplied >= rf.commitIndex && !rf.killed() {
			rf.applyCond.Wait()
		}
		if rf.killed() {
			rf.mu.Unlock()
			return
		}
		start := rf.lastApplied + 1
		end := rf.commitIndex
		batch := append([]LogEntry(nil), rf.log[start:end+1]...)
		rf.lastApplied = end
		rf.mu.Unlock()

		for i, entry := range batch {
			rf.applyCh <- ApplyMsg{
				CommandValid: true,
				Command:      entry.Command,
				CommandIndex: start + i,
			}
		}
	}
}

// Wake the applier so a killed peer's goroutine can exit rather than leaking.
func (rf *Raft) wake() {
	rf.mu.Lock()
	rf.applyCond.Broadcast()
	rf.mu.Unlock()
}
