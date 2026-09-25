// Tests cover validation, orchestration, recovery, and evidence handling.
package main

import (
	"context"
	"encoding/csv"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// TestHealthPreservesDiagnosticWhenRetryIsCanceled keeps the first useful error after cancellation.
func TestHealthPreservesDiagnosticWhenRetryIsCanceled(t *testing.T) {
	for _, firstRequest := range []bool{false, true} {
		t.Run(fmt.Sprintf("first_request=%t", firstRequest), func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			var requests atomic.Int32
			wrongSHA := strings.Repeat("b", 40)
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				// Cancel on the first or second request to exercise both retry paths.
				if requests.Add(1) == 1 && !firstRequest {
					fmt.Fprintf(w, `{"status":"ok","commit_sha":%q}`, wrongSHA)
					return
				}
				cancel()
				<-r.Context().Done()
			}))
			defer srv.Close()
			served, err := health(ctx, srv.URL, strings.Repeat("a", 40))
			wantSHA, wantDiagnostic, wantRequests := wrongSHA, "health response did not match expected status and revision", int32(2)
			if firstRequest {
				wantSHA, wantDiagnostic, wantRequests = "", "HTTP request failed", 1
			}
			if requests.Load() != wantRequests || served != wantSHA || err == nil || !strings.Contains(err.Error(), wantDiagnostic) {
				t.Fatalf("requests=%d served=%q err=%v", requests.Load(), served, err)
			}
		})
	}
}

// TestArguments accepts canonical identities and rejects unsafe input variants.
func TestArguments(t *testing.T) {
	sha := strings.Repeat("a", 40)
	digest := "ghcr.io/example-owner/previewmesh-c34-r12@sha256:" + strings.Repeat("b", 64)
	good := []string{"deploy", "--repository-id", "12", "--pr", "3", "--source-repository", "owner/demo", "--sha", sha, "--image", digest}
	if _, err := parse(good); err != nil {
		t.Fatal(err)
	}
	// Each variant violates one identity, immutability, or bounds check.
	for _, extra := range [][]string{{"--repository-id", "012"}, {"--pr", "0"}, {"--pr", "1;id"}, {"--sha", "main"}, {"--image", "ghcr.io/example-owner/previewmesh-c34-r12:latest"}, {"--image", strings.Replace(digest, "r12", "r13", 1)}, {"--image", strings.Replace(digest, "c34", "c0", 1)}, {"--image", strings.Replace(digest, "c34", "c034", 1)}, {"--image", strings.Replace(digest, "c34-", "", 1)}, {"--hostname", "evil.test"}, {"--port", "65536"}, {"--timeout", "0"}, {"--pull-secret", "--all"}} {
		args := append(append([]string{}, good...), extra...)
		if _, err := parse(args); err == nil {
			t.Errorf("accepted %v", extra)
		}
	}
	a, _ := identity("12", "3")
	b, _ := identity("13", "3")
	if a == b {
		t.Fatal("repository namespaces collide")
	}
}

// TestHealth covers successful, malformed, unhealthy, and mismatched responses.
func TestHealth(t *testing.T) {
	sha := strings.Repeat("a", 40)
	for _, tc := range []struct {
		name, body string
		code       int
		ok         bool
	}{
		{"match", `{"status":"ok","commit_sha":"` + sha + `"}`, 200, true},
		{"wrong revision", `{"status":"ok","commit_sha":"` + strings.Repeat("b", 40) + `"}`, 200, false},
		{"unhealthy", `{"status":"bad","commit_sha":"` + sha + `"}`, 200, false},
		{"http failure", `{"status":"ok","commit_sha":"` + sha + `"}`, 503, false},
		{"malformed", `oops`, 200, false},
		{"trailing garbage", `{"status":"ok","commit_sha":"` + sha + `"}oops`, 200, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/health" {
					t.Error(r.URL.Path)
				}
				w.WriteHeader(tc.code)
				fmt.Fprint(w, tc.body)
			}))
			defer srv.Close()
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
			defer cancel()
			served, err := health(ctx, srv.URL, sha)
			if (err == nil) != tc.ok {
				t.Fatalf("served=%q err=%v", served, err)
			}
		})
	}
}

// TestRevisionVerification keeps an exact source revision distinct from an arbitrary valid SHA.
func TestRevisionVerification(t *testing.T) {
	sha := strings.Repeat("a", 40)
	for _, tc := range []struct {
		name, served, want string
	}{
		{"matching", sha, "success"},
		{"mismatched", strings.Repeat("b", 40), "failure"},
		{"malformed", "main", "not_checked"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := revisionVerification(sha, tc.served); got != tc.want {
				t.Fatalf("revisionVerification(%q, %q) = %q, want %q", sha, tc.served, got, tc.want)
			}
		})
	}
}

// TestHTTPRevisionMismatchIsRecorded ensures a reachable but stale preview cannot look verified.
func TestHTTPRevisionMismatchIsRecorded(t *testing.T) {
	wanted := strings.Repeat("a", 40)
	served := strings.Repeat("b", 40)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, `{"status":"ok","commit_sha":%q}`, served)
	}))
	defer srv.Close()
	x := runner{o: options{httpTimeout: 20 * time.Millisecond}, r: result{URL: srv.URL}}
	if err := x.verifyHTTP(wanted); err == nil {
		t.Fatal("stale revision was accepted")
	}
	if x.r.ServedSHA != served || x.r.HTTPVerification != "failure" || x.r.RevisionVerification != "failure" {
		t.Fatalf("revision evidence = served %q, HTTP %q, revision %q", x.r.ServedSHA, x.r.HTTPVerification, x.r.RevisionVerification)
	}
}

// fakeTools exercises orchestration failures without requiring a live cluster.
func fakeTools(t *testing.T, nsJSON string, helmFail bool) *runner {
	t.Helper()
	dir := t.TempDir()
	log := filepath.Join(dir, "calls")
	t.Setenv("CALLS", log)
	t.Setenv("NAMESPACE_JSON", nsJSON)
	t.Setenv("HELM_FAIL", "0")
	if helmFail {
		t.Setenv("HELM_FAIL", "1")
	}
	script := `#!/bin/sh
# Record each fake command so tests can assert the orchestration order.
printf '%s\n' "$*" >> "$CALLS"
case "$1 $2" in
 'get namespace') if [ "$API_FAIL" = 1 ]; then exit 1; fi; printf '%s' "$NAMESPACE_JSON";;
 'rollout status') if [ "$ROLLOUT_FAIL" = 1 ]; then exit 1; fi; exit 0;;
 'wait --for=condition=Ready') if [ "$POD_READY_FAIL" = 1 ]; then exit 1; fi; exit 0;;
 'wait --for=jsonpath={.spec.clusterIP}') if [ "$SERVICE_READY_FAIL" = 1 ]; then exit 1; fi; exit 0;;
 'wait --for=jsonpath={.status.loadBalancer.ingress}') if [ "$INGRESS_READY_FAIL" = 1 ]; then exit 1; fi; exit 0;;
 'status pm-r12-pr3') printf '{"version":2}';;
 'upgrade --install') exit "$HELM_FAIL";;
esac
`
	for _, tool := range []string{"kubectl", "helm"} {
		if err := os.WriteFile(filepath.Join(dir, tool), []byte(script), 0700); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	return &runner{o: options{repoID: "12", pr: "3", sha: strings.Repeat("b", 40), source: "owner/demo", port: 8080, timeout: time.Second, httpTimeout: 30 * time.Millisecond, chart: "unused"}, r: result{Namespace: "pm-r12-pr3"}}
}

// TestReadinessChecksAllResources requires every routing layer before HTTP verification.
func TestReadinessChecksAllResources(t *testing.T) {
	x := fakeTools(t, "", false)
	var requests atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		fmt.Fprintf(w, `{"status":"ok","commit_sha":%q}`, x.o.sha)
	}))
	defer srv.Close()
	x.r.URL = srv.URL
	if err := x.check(x.o.sha); err != nil {
		t.Fatal(err)
	}
	calls, err := os.ReadFile(os.Getenv("CALLS"))
	if err != nil {
		t.Fatal(err)
	}
	callText := string(calls)
	rollout := "rollout status deployment/pm-r12-pr3 -n pm-r12-pr3 --timeout=1s"
	pods := "wait --for=condition=Ready pod -l app.kubernetes.io/instance=pm-r12-pr3 -n pm-r12-pr3 --timeout=1s"
	service := "wait --for=jsonpath={.spec.clusterIP} service/pm-r12-pr3 -n pm-r12-pr3 --timeout=1s"
	ingress := "wait --for=jsonpath={.status.loadBalancer.ingress} ingress/pm-r12-pr3 -n pm-r12-pr3 --timeout=1s"
	checks := []string{rollout, pods, service, ingress}
	for i, check := range checks {
		if strings.Index(callText, check) < 0 {
			t.Fatalf("readiness check missing: %s\ncalls: %s", check, callText)
		}
		if i > 0 && strings.Index(callText, checks[i-1]) > strings.Index(callText, check) {
			t.Fatalf("readiness checks out of order: %s", callText)
		}
	}
	if strings.Index(callText, ingress) < strings.Index(callText, service) {
		t.Fatalf("readiness checks missing or out of order: %s", callText)
	}
	if requests.Load() != 1 {
		t.Fatalf("HTTP verification requests=%d, want 1", requests.Load())
	}
	if x.r.HTTPStatus != http.StatusOK || x.r.HTTPVerification != "success" || x.r.ServedSHA != x.o.sha || x.r.RevisionVerification != "success" {
		t.Fatalf("HTTP evidence = status %d, HTTP verification %q, served SHA %q, revision verification %q", x.r.HTTPStatus, x.r.HTTPVerification, x.r.ServedSHA, x.r.RevisionVerification)
	}
}

// TestReadinessFailureStopsHTTPVerification keeps an unready preview from being reported as healthy.
func TestReadinessFailureStopsHTTPVerification(t *testing.T) {
	for _, tc := range []struct {
		name, variable, want string
	}{
		{"deployment", "ROLLOUT_FAIL", "deployment readiness check failed"},
		{"pod", "POD_READY_FAIL", "pod readiness check failed"},
		{"service", "SERVICE_READY_FAIL", "service readiness check failed"},
		{"ingress", "INGRESS_READY_FAIL", "ingress readiness check failed"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			x := fakeTools(t, "", false)
			t.Setenv(tc.variable, "1")
			var requests atomic.Int32
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				requests.Add(1)
				fmt.Fprintf(w, `{"status":"ok","commit_sha":%q}`, x.o.sha)
			}))
			defer srv.Close()
			x.r.URL = srv.URL
			err := x.check(x.o.sha)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error=%v, want %q", err, tc.want)
			}
			if requests.Load() != 0 {
				t.Fatalf("HTTP verification started after readiness failure: %d requests", requests.Load())
			}
		})
	}
}

// ownedNS returns the smallest owned namespace payload needed by recovery tests.
func ownedNS(sha string) string {
	v := map[string]any{"metadata": map[string]any{"uid": "uid-1", "labels": map[string]string{domain + "managed-by": "previewmesh", domain + "repository-id": "12", domain + "pr-number": "3"}, "annotations": map[string]string{domain + "verified-sha": sha, domain + "verified-revision": "1"}}}
	b, _ := json.Marshal(v)
	return string(b)
}

// TestCleanupRefusesUnknownOwnershipAndAPIFailure avoids deleting unknown namespaces.
func TestCleanupRefusesUnknownOwnershipAndAPIFailure(t *testing.T) {
	for _, tc := range []struct {
		name, ns        string
		apiFail, wantOK bool
	}{
		{"absent", "", false, true}, {"api outage", "", true, false}, {"unowned", `{"metadata":{"labels":{}}}`, false, false}, {"still present", ownedNS(strings.Repeat("a", 40)), false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			x := fakeTools(t, tc.ns, false)
			if tc.apiFail {
				t.Setenv("API_FAIL", "1")
			}
			err := x.cleanup()
			if (err == nil) != tc.wantOK {
				t.Fatalf("err=%v", err)
			}
			calls, _ := os.ReadFile(os.Getenv("CALLS"))
			if tc.name != "still present" && strings.Contains(string(calls), "delete") {
				t.Fatal("unexpected deletion")
			}
			if tc.wantOK && x.r.Cleanup != "confirmed_absent" {
				t.Fatal("absence not recorded")
			}
			if !tc.wantOK && x.r.Cleanup != "failure" {
				t.Fatalf("cleanup failure was not recorded: %q", x.r.Cleanup)
			}
		})
	}
}

// TestCleanupIsSafeWhenNamespaceIsAlreadyAbsent repeats cleanup without a live cluster.
func TestCleanupIsSafeWhenNamespaceIsAlreadyAbsent(t *testing.T) {
	x := fakeTools(t, "", false)
	for attempt := 1; attempt <= 2; attempt++ {
		if err := x.cleanup(); err != nil {
			t.Fatalf("cleanup attempt %d: %v", attempt, err)
		}
		if x.r.Cleanup != "confirmed_absent" || x.r.FailedStage != "" {
			t.Fatalf("cleanup attempt %d result = %q, failed stage = %q", attempt, x.r.Cleanup, x.r.FailedStage)
		}
	}

	calls, err := os.ReadFile(os.Getenv("CALLS"))
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Count(string(calls), "get namespace"); got != 2 {
		t.Fatalf("cleanup should only recheck the already-absent namespace, got %d reads", got)
	}
	if strings.Contains(string(calls), "delete") {
		t.Fatalf("cleanup attempted deletion after absence was confirmed: %s", calls)
	}
	counts := map[string]int{}
	for _, timing := range x.r.StageTimings {
		counts[timing.Stage]++
	}
	if counts["cleanup"] != 2 || counts["resource_verify"] != 2 {
		t.Fatalf("repeat cleanup timing stages = %#v", counts)
	}
}

// TestCleanupDeletesOwnedNamespaceAndConfirmsAbsence covers the successful close path.
func TestCleanupDeletesOwnedNamespaceAndConfirmsAbsence(t *testing.T) {
	x := fakeTools(t, ownedNS(strings.Repeat("a", 40)), false)
	dir := strings.Split(os.Getenv("PATH"), string(os.PathListSeparator))[0]
	deleted := filepath.Join(dir, "deleted")
	t.Setenv("DELETED_MARKER", deleted)
	script := `#!/bin/sh
printf '%s\n' "$*" >> "$CALLS"
case "$1" in
get)
  if [ -f "$DELETED_MARKER" ]; then exit 0; fi
  printf '%s' "$NAMESPACE_JSON"
  ;;
delete)
  : > "$DELETED_MARKER"
  ;;
wait)
  ;;
esac
`
	if err := os.WriteFile(filepath.Join(dir, "kubectl"), []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	if err := x.cleanup(); err != nil {
		t.Fatal(err)
	}
	if x.r.Cleanup != "confirmed_absent" || x.r.FailedStage != "" {
		t.Fatalf("cleanup result = %q, failed stage = %q", x.r.Cleanup, x.r.FailedStage)
	}
	calls, err := os.ReadFile(os.Getenv("CALLS"))
	if err != nil {
		t.Fatal(err)
	}
	callText := string(calls)
	if strings.Count(callText, "get namespace") != 2 {
		t.Fatalf("cleanup should inspect before and after deletion: %s", callText)
	}
	counts := map[string]int{}
	for _, timing := range x.r.StageTimings {
		counts[timing.Stage]++
	}
	if counts["cleanup"] != 1 || counts["resource_verify"] != 2 {
		t.Fatalf("cleanup timing stages = %#v", counts)
	}
	for _, want := range []string{
		"delete --raw /api/v1/namespaces/pm-r12-pr3 -f -",
		"wait --for=delete namespace/pm-r12-pr3 --timeout=1s",
	} {
		if !strings.Contains(callText, want) {
			t.Fatalf("missing cleanup operation %q in %s", want, callText)
		}
	}
}

// TestRollbackMustVerifyOldRevision requires the previous revision to pass health checks.
func TestRollbackMustVerifyOldRevision(t *testing.T) {
	old := strings.Repeat("a", 40)
	for _, valid := range []bool{true, false} {
		t.Run(fmt.Sprint(valid), func(t *testing.T) {
			x := fakeTools(t, ownedNS(old), true)
			responseSHA := old
			if !valid {
				responseSHA = strings.Repeat("c", 40)
			}
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				fmt.Fprintf(w, `{"status":"ok","commit_sha":%q}`, responseSHA)
			}))
			defer srv.Close()
			x.r.URL = srv.URL
			if err := x.deploy(); err == nil {
				t.Fatal("failed update must stay failed even if rollback succeeds")
			}
			observedRuntime := false
			for _, timing := range x.r.StageTimings {
				if timing.Stage == "resource_observation" && timing.StartedAtUTC != "" && timing.EndedAtUTC != "" {
					observedRuntime = true
				}
			}
			if !observedRuntime {
				t.Fatalf("runtime snapshot timing missing: %#v", x.r.StageTimings)
			}
			if valid && (x.r.Rollback != "verified" || x.r.ServedSHA != old) {
				t.Fatalf("%+v", x.r)
			}
			if !valid && x.r.Rollback != "failed" {
				t.Fatalf("%+v", x.r)
			}
			calls, _ := os.ReadFile(os.Getenv("CALLS"))
			if !strings.Contains(string(calls), "rollback pm-r12-pr3 1") {
				t.Fatal("rollback not attempted")
			}
		})
	}
}

// TestEvidenceEscapesCSV keeps stage errors safe in the CSV evidence file.
func TestEvidenceEscapesCSV(t *testing.T) {
	x := runner{o: options{repoID: "12", pr: "3", source: "owner/repo", evidence: filepath.Join(t.TempDir(), "attempt.csv")}}
	err := x.stage("verify", func() error { return fmt.Errorf("bad, result\nsecond line") })
	if err == nil {
		t.Fatal("failure lost")
	}
	f, err := os.Open(x.o.evidence)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	rows, err := csv.NewReader(f).ReadAll()
	if err != nil || len(rows) != 2 || len(rows[1]) != 13 || rows[1][11] != "failure" {
		t.Fatalf("%v %v", rows, err)
	}
	start, startErr := time.Parse(time.RFC3339Nano, rows[1][8])
	end, endErr := time.Parse(time.RFC3339Nano, rows[1][9])
	if startErr != nil || endErr != nil || end.Before(start) {
		t.Fatalf("invalid CSV stage timestamps: start=%q end=%q (%v, %v)", rows[1][8], rows[1][9], startErr, endErr)
	}
	if len(x.r.StageTimings) != 1 || x.r.StageTimings[0].Stage != "verify" || x.r.StageTimings[0].Result != "failure" {
		t.Fatalf("stage timing not included in result: %#v", x.r.StageTimings)
	}
	payload, err := json.Marshal(x.r)
	if err != nil || !strings.Contains(string(payload), `"stage_timings":[`) || !strings.Contains(string(payload), rows[1][8]) {
		t.Fatalf("JSON result missing stage timestamps: %s (%v)", payload, err)
	}
}

// TestIgnoredBuildFilesRejected treats ignored files as unclean build input.
func TestIgnoredBuildFilesRejected(t *testing.T) {
	x := fakeTools(t, "", false)
	dir := strings.Split(os.Getenv("PATH"), string(os.PathListSeparator))[0]
	git := `#!/bin/sh
case "$3" in
 rev-parse) printf '%s\n' "$EXPECTED_SHA";;
 status) case "$*" in *--ignored*) printf '!! .env\n';; esac;;
esac
`
	t.Setenv("EXPECTED_SHA", x.o.sha)
	if err := os.WriteFile(filepath.Join(dir, "git"), []byte(git), 0700); err != nil {
		t.Fatal(err)
	}
	x.o.sourceDir = t.TempDir()
	err := x.build()
	if err == nil || !strings.Contains(err.Error(), "clean") {
		t.Fatalf("ignored file not rejected: %v", err)
	}
}

// TestInheritedPipeDoesNotDefeatTimeout bounds commands that leave child pipes open.
func TestInheritedPipeDoesNotDefeatTimeout(t *testing.T) {
	x := runner{o: options{timeout: 20 * time.Millisecond}}
	start := time.Now()
	_, err := x.run(nil, "sh", "-c", "sleep 2 & wait")
	if err == nil || time.Since(start) > 1500*time.Millisecond {
		t.Fatalf("unbounded command wait: %v (%s)", err, time.Since(start))
	}
}

// TestUIDConflictCannotReportCleanupSuccess rejects deletion when the UID precondition fails.
func TestUIDConflictCannotReportCleanupSuccess(t *testing.T) {
	x := fakeTools(t, ownedNS(strings.Repeat("a", 40)), false)
	dir := strings.Split(os.Getenv("PATH"), string(os.PathListSeparator))[0]
	script := `#!/bin/sh
printf '%s\n' "$*" >> "$CALLS"
case "$1" in
 get) printf '%s' "$NAMESPACE_JSON";;
 delete) cat > "$DELETE_BODY"; exit 1;;
esac
`
	t.Setenv("DELETE_BODY", filepath.Join(dir, "delete.json"))
	os.WriteFile(filepath.Join(dir, "kubectl"), []byte(script), 0700)
	if err := x.cleanup(); err == nil || x.r.Cleanup == "confirmed_absent" {
		t.Fatal("UID conflict treated as success")
	}
	b, err := os.ReadFile(os.Getenv("DELETE_BODY"))
	if err != nil {
		t.Fatal(err)
	}
	var body struct {
		Preconditions struct {
			UID string `json:"uid"`
		} `json:"preconditions"`
	}
	if json.Unmarshal(b, &body) != nil || body.Preconditions.UID != "uid-1" {
		t.Fatalf("missing UID precondition: %s", b)
	}
}
