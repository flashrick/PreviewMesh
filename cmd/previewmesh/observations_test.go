package main

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// TestRuntimeObservations distinguishes presence, readiness, and unavailable data.
func TestRuntimeObservations(t *testing.T) {
	for _, tc := range []struct{ kind, data, want string }{
		{"deployment", `{"metadata":{"name":"preview","generation":3},"spec":{"replicas":2},"status":{"readyReplicas":1,"updatedReplicas":1,"availableReplicas":1,"observedGeneration":2}}`, "ready 1/2; updated 1; available 1; observed generation 2/3"},
		{"pods", `{"items":[{"status":{"phase":"Running","conditions":[{"type":"Ready","status":"False"}],"containerStatuses":[{"state":{"waiting":{"reason":"CrashLoopBackOff"}}}]}}]}`, "ready 0/1; Running 1; CrashLoopBackOff"},
		{"pods", `{"items":[]}`, "no matching pods"},
		{"service", `{"metadata":{"name":"preview"},"spec":{"clusterIP":"10.43.0.2"}}`, "present; ClusterIP 10.43.0.2"},
		{"ingress", `{"metadata":{"name":"preview"},"spec":{"rules":[{"host":"preview.test"}]}}`, "present; hosts preview.test"},
		{"deployment", `{}`, "unavailable (resource not returned)"},
		{"pods", `invalid`, "unavailable (invalid API response)"},
	} {
		if got := summarizeRuntime(tc.kind, []byte(tc.data)); got != tc.want {
			t.Errorf("%s: got %q want %q", tc.kind, got, tc.want)
		}
	}
}

// TestHTTPObservation keeps a 200 response distinct from a verified commit.
func TestHTTPObservation(t *testing.T) {
	for _, code := range []int{200, 503} {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(code)
			fmt.Fprintf(w, `{"status":"ok","commit_sha":%q}`, strings.Repeat("b", 40))
		}))
		ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
		status := 0
		_, err := healthObserved(ctx, srv.URL, strings.Repeat("a", 40), &status)
		cancel()
		srv.Close()
		if status != code || err == nil {
			t.Fatalf("status=%d err=%v", status, err)
		}
	}
}
