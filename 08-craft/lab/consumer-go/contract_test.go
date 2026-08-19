// Topic 6: the consumer's side of the contract, checked against a STUB built
// from the committed snapshot -- not against the live service.
//
// WHAT THIS DEMONSTRATES: running the consumers against the live API is an
// integration test, not a contract test, and it will not survive the two
// services being deployed independently. The stub below serves exactly what the
// committed snapshot promises, so this test passes or fails based on the
// contract alone.
//
//	go test ./...
package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

// stubFromSnapshot serves a response that the committed openapi.snapshot.json
// declares to be valid. Nothing here talks to the API.
func stubFromSnapshot(t *testing.T, body string) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(body))
	}))
}

func TestConsumerParsesTheContractedShape(t *testing.T) {
	srv := stubFromSnapshot(t, `{"items":[{"id":1,"status":"paid","total_cents":500}],"total":1}`)
	defer srv.Close()

	out, _, err := newClient(srv.URL).listOrders(context.Background(), 1)
	if err != nil {
		t.Fatalf("contracted shape did not parse: %v", err)
	}
	if out.Total != 1 || len(out.Items) != 1 || out.Items[0].TotalCents != 500 {
		t.Fatalf("parsed the wrong values: %+v", out)
	}
}

func TestBreakOne_TotalIntToString(t *testing.T) {
	// Topic 6, break 1. The provider changed `total: int` to `total: str`.
	// The Go client has NOT been regenerated, so this is what a deployed
	// consumer sees: a runtime decode error, immediately, naming the field.
	srv := stubFromSnapshot(t, `{"items":[],"total":"1"}`)
	defer srv.Close()

	_, _, err := newClient(srv.URL).listOrders(context.Background(), 1)
	if err == nil {
		t.Fatal("expected a decode error; encoding/json is strict about types " +
			"even though it is permissive about missing fields")
	}
	t.Logf("caught at runtime: %v", err)
}

func TestBreakTwo_RequiredFieldBecomesOptional(t *testing.T) {
	// Topic 6, break 2, and the important one. `total` is simply absent.
	// This is NOT breaking for the provider and IS breaking for this consumer --
	// and NOTHING catches it. Go's json decoder leaves the field at its zero
	// value, the build is green, the test below passes, and the consumer now
	// silently reports a total of 0.
	srv := stubFromSnapshot(t, `{"items":[{"id":1,"status":"paid","total_cents":500}]}`)
	defer srv.Close()

	out, _, err := newClient(srv.URL).listOrders(context.Background(), 1)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if out.Total != 0 {
		t.Fatalf("expected the zero value, got %d", out.Total)
	}
	t.Log("PASSED, and that is the finding: an omitted required field reads as 0. " +
		"Neither the compiler nor a schema differ can see this. A consumer-driven " +
		"contract (Pact) can, because the consumer RECORDED that it reads `total`.")
}

func TestSnapshotDeclaresWhatThisConsumerReads(t *testing.T) {
	// The closest thing to a Pact expectation without a broker: assert, from the
	// consumer's own repository, that the committed contract still declares the
	// fields this consumer actually reads. It is thirty lines and it catches
	// break 2, which is the one everything else missed.
	needed := map[string][]string{
		"CustomerOrderListOut": {"items", "total"},
		"CustomerOrderOut":     {"id", "status", "total_cents"},
	}
	raw, err := readSnapshot()
	if err != nil {
		t.Skipf("snapshot not readable from the test working directory: %v", err)
	}
	var doc struct {
		Components struct {
			Schemas map[string]struct {
				Required []string `json:"required"`
			} `json:"schemas"`
		} `json:"components"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("snapshot is not valid JSON: %v", err)
	}
	for schema, fields := range needed {
		got, ok := doc.Components.Schemas[schema]
		if !ok {
			t.Fatalf("the contract no longer declares %s", schema)
		}
		have := map[string]bool{}
		for _, f := range got.Required {
			have[f] = true
		}
		for _, f := range fields {
			if !have[f] {
				t.Errorf("%s.%s is no longer REQUIRED in the contract, and this "+
					"consumer depends on it", schema, f)
			}
		}
	}
}
