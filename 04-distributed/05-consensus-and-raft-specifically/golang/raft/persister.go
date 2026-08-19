// Layer 4 · Topic 5 — durable state that survives a crash.
//
// The Persister is deliberately a byte slice, not a struct. Raft's persistence
// requirement is not "keep these three fields somewhere" -- it is "these three
// fields must be on stable storage BEFORE you reply to an RPC that depends on
// them". Forcing an explicit encode/decode makes the save points visible in
// raft.go, which is where the ordering bug lives if you have one.
//
// Lab 3C's characteristic failure: everything passes, then one run in two
// hundred fails, because a vote was granted and the process died before
// votedFor reached disk. Restart, and the node votes again in the same term.
// Two leaders in one term, and every safety argument in the paper collapses.
package raft

import (
	"bytes"
	"encoding/gob"
	"sync"
)

type Persister struct {
	mu    sync.Mutex
	state []byte
}

func NewPersister() *Persister { return &Persister{} }

func (p *Persister) Save(state []byte) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.state = append([]byte(nil), state...)
}

func (p *Persister) Read() []byte {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]byte(nil), p.state...)
}

func (p *Persister) Size() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return len(p.state)
}

// persistentState is exactly Figure 2's list and nothing else. commitIndex and
// lastApplied are deliberately absent: they are volatile, and a node that
// persisted commitIndex would be asserting on restart something it can only
// learn from the leader.
type persistentState struct {
	CurrentTerm int
	VotedFor    int
	Log         []LogEntry
}

func encodeState(s persistentState) []byte {
	var buf bytes.Buffer
	if err := gob.NewEncoder(&buf).Encode(s); err != nil {
		panic("raft: cannot encode persistent state: " + err.Error())
	}
	return buf.Bytes()
}

func decodeState(data []byte) (persistentState, bool) {
	if len(data) == 0 {
		return persistentState{}, false
	}
	var s persistentState
	if err := gob.NewDecoder(bytes.NewReader(data)).Decode(&s); err != nil {
		return persistentState{}, false
	}
	return s, true
}
