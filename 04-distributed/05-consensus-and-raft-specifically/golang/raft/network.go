// Layer 4 · Topic 5 — the unreliable network the Raft peers talk over.
//
// WHAT THIS IS: a stand-in for MIT 6.5840's `labrpc`. It exists so the tests can
// partition, drop, delay and crash without any of it being visible to raft.go --
// which is the point. An implementation that only works when you can see the
// whole cluster is not a consensus implementation.
//
// Three properties that matter, and are easy to get wrong in a fake network:
//
//  1. Every call is delivered on its own goroutine after a random delay. A
//     synchronous in-process call would hide every ordering bug Raft has,
//     because the reply would always arrive before anything else could happen.
//  2. Arguments and replies are DEEP COPIED across the boundary. Sharing a slice
//     between caller and handler would let a leader mutate a follower's log
//     through aliasing, which no real RPC can do and which would make a broken
//     implementation pass.
//  3. Reachability is checked twice -- once before the handler runs and once
//     before the reply is delivered. That is what makes "the request arrived but
//     the reply was lost" expressible, which is Topic 1's whole point and the
//     hardest case for a Raft leader to handle correctly.
package raft

import (
	"math/rand"
	"sync"
	"time"
)

type Network struct {
	mu       sync.Mutex
	nodes    []*Raft
	group    []int  // nodes in different groups cannot reach each other
	up       []bool // false once a node is crashed
	reliable bool
	rpcs     int
}

func NewNetwork(n int) *Network {
	net := &Network{
		nodes:    make([]*Raft, n),
		group:    make([]int, n),
		up:       make([]bool, n),
		reliable: true,
	}
	for i := range net.up {
		net.up[i] = true
	}
	return net
}

func (n *Network) attach(i int, rf *Raft) {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.nodes[i] = rf
	n.up[i] = true
	n.group[i] = 0
}

func (n *Network) size() int { return len(n.nodes) }

// SetReliable(false) drops roughly one call in ten and lengthens delays.
func (n *Network) SetReliable(reliable bool) {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.reliable = reliable
}

// Disconnect isolates one node from every other node. Its timers keep running,
// which is exactly the situation that produces a stale leader.
func (n *Network) Disconnect(i int) {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.group[i] = -(i + 1) // a group of one, unreachable from anything else
}

func (n *Network) Connect(i int) {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.group[i] = 0
}

// Partition splits the cluster into groups. Nodes reach each other iff they are
// in the same group. Nodes not named stay where they are.
func (n *Network) Partition(groups ...[]int) {
	n.mu.Lock()
	defer n.mu.Unlock()
	for g, members := range groups {
		for _, i := range members {
			n.group[i] = g + 1
		}
	}
}

func (n *Network) setDown(i int, down bool) {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.up[i] = !down
}

func (n *Network) reachable(from, to int) bool {
	n.mu.Lock()
	defer n.mu.Unlock()
	return n.up[from] && n.up[to] && n.group[from] == n.group[to]
}

func (n *Network) target(i int) *Raft {
	n.mu.Lock()
	defer n.mu.Unlock()
	return n.nodes[i]
}

func (n *Network) rpcCount() int {
	n.mu.Lock()
	defer n.mu.Unlock()
	return n.rpcs
}

func (n *Network) lossy() (bool, bool) {
	n.mu.Lock()
	defer n.mu.Unlock()
	return !n.reliable, n.reliable
}

// Call delivers one RPC. It returns false for anything the caller cannot
// distinguish from the peer being unreachable -- a dropped request, a dropped
// reply, or a crashed peer. From raft.go's point of view all of those are the
// same event, which is correct: they are.
func (n *Network) Call(from, to int, method string, args interface{}, reply interface{}) bool {
	n.mu.Lock()
	n.rpcs++
	n.mu.Unlock()

	unreliable, _ := n.lossy()

	delay := time.Duration(1+rand.Intn(12)) * time.Millisecond
	if unreliable {
		delay = time.Duration(1+rand.Intn(40)) * time.Millisecond
	}
	time.Sleep(delay)

	if !n.reachable(from, to) {
		return false
	}
	if unreliable && rand.Intn(100) < 10 {
		return false // request lost
	}

	peer := n.target(to)
	if peer == nil || peer.killed() {
		return false
	}

	switch method {
	case "Raft.RequestVote":
		a := *(args.(*RequestVoteArgs))
		var r RequestVoteReply
		peer.RequestVote(&a, &r)
		if !n.reachable(to, from) {
			return false // the reply was lost: the caller learns nothing
		}
		if unreliable && rand.Intn(100) < 10 {
			return false
		}
		*(reply.(*RequestVoteReply)) = r
	case "Raft.AppendEntries":
		a := *(args.(*AppendEntriesArgs))
		// Deep copy: the handler must not be able to see later mutations of the
		// leader's slice, and the leader must not see the follower's.
		a.Entries = append([]LogEntry(nil), a.Entries...)
		var r AppendEntriesReply
		peer.AppendEntries(&a, &r)
		if !n.reachable(to, from) {
			return false
		}
		if unreliable && rand.Intn(100) < 10 {
			return false
		}
		*(reply.(*AppendEntriesReply)) = r
	default:
		return false
	}
	return true
}
