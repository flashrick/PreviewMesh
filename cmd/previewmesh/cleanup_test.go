package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// recoveryTools models an interrupted wait, a stuck finalizer, and Namespace replacement.
func recoveryTools(t *testing.T, mode string) *runner {
	t.Helper()
	x := fakeTools(t, ownedNS(strings.Repeat("a", 40)), false)
	x.o.namespaceUID, x.o.cleanupAttempts = "uid-1", 3
	dir := strings.Split(os.Getenv("PATH"), string(os.PathListSeparator))[0]
	t.Setenv("RECOVERY_MODE", mode)
	t.Setenv("RECOVERY_STATE", filepath.Join(dir, "state"))
	script := `#!/bin/sh
printf '%s\n' "$*" >> "$CALLS"
case "$1 $2" in
 'get namespace')
  [ "$RECOVERY_MODE" = api-error ] && exit 1
  [ "$RECOVERY_MODE" = absent ] && exit 0
  if [ -f "$RECOVERY_STATE.done" ]; then exit 0; fi
  if [ "$RECOVERY_MODE" = replacement ] && [ -f "$RECOVERY_STATE" ]; then
   printf '%s' "$NAMESPACE_JSON" | sed 's/uid-1/uid-2/g'
  else printf '%s' "$NAMESPACE_JSON"; fi;;
 'delete --raw')
  cat >> "$CALLS"
  touch "$RECOVERY_STATE"
  [ "$RECOVERY_MODE" = conflict ] && exit 1
  exit 0;;
 'wait --for=delete')
  if [ "$RECOVERY_MODE" = interrupted ]; then
   if [ -f "$RECOVERY_STATE.waited" ]; then touch "$RECOVERY_STATE.done"; exit 0; fi
   touch "$RECOVERY_STATE.waited"
  fi
  exit 1;;
 'api-resources --namespaced=true') printf 'pods\nsecrets\n';;
 'get pods') printf '{"items":[{"metadata":{"name":"blocked-pod","uid":"pod-1","finalizers":["example.test/hold"],"deletionTimestamp":"2026-09-28T00:00:00Z"}}]}';;
 'get secrets')
  [ "$RECOVERY_MODE" = forbidden ] && exit 1
  [ "$RECOVERY_MODE" = malformed ] && { printf '{}'; exit 0; }
  printf '{"items":[{"metadata":{"name":"helm-history","uid":"secret-1"},"data":{"token":"DO_NOT_EMIT"}}]}';;
 *) exit 1;;
esac
`
	if err := os.WriteFile(filepath.Join(dir, "kubectl"), []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	return x
}

func TestCleanupRetryRecovery(t *testing.T) {
	for _, mode := range []string{"interrupted", "absent", "stuck", "replacement", "conflict", "api-error"} {
		t.Run(mode, func(t *testing.T) {
			x := recoveryTools(t, mode)
			err := x.retryCleanup()
			success := mode == "interrupted" || mode == "absent"
			if (err == nil) != success {
				t.Fatalf("err=%v result=%+v", err, x.r)
			}
			if success && (x.r.Cleanup != "confirmed_absent" || x.r.FailedStage != "" || x.r.Inspection.State != "confirmed_absent") {
				t.Fatalf("result=%+v", x.r)
			}
			if !success && x.r.Cleanup != "failure" {
				t.Fatal(x.r.Cleanup)
			}
			wantAttempts := 3
			if mode == "interrupted" {
				wantAttempts = 2
			}
			if mode == "absent" {
				wantAttempts = 1
			}
			if len(x.r.Recovery.Attempts) != wantAttempts {
				t.Fatal(x.r.Recovery)
			}
			if mode == "interrupted" && x.r.Recovery.Attempts[0].Error == "" {
				t.Fatal("lost interrupted attempt")
			}
			calls, _ := os.ReadFile(os.Getenv("CALLS"))
			if mode == "replacement" && strings.Count(string(calls), "delete --raw") != 1 {
				t.Fatalf("deleted replacement: %s", calls)
			}
			if mode == "absent" && strings.Contains(string(calls), "delete") {
				t.Fatalf("deleted absent namespace: %s", calls)
			}
			if mode == "interrupted" && !strings.Contains(string(calls), `"preconditions":{"uid":"uid-1"}`) {
				t.Fatal("missing UID precondition")
			}
		})
	}
}

func TestCleanupInspection(t *testing.T) {
	for _, mode := range []string{"stuck", "forbidden", "malformed", "api-error"} {
		t.Run(mode, func(t *testing.T) {
			x := recoveryTools(t, mode)
			err := x.inspectCleanup()
			if (err == nil) != (mode == "stuck") {
				t.Fatal(err)
			}
			b, _ := json.Marshal(x.r.Inspection)
			if strings.Contains(string(b), "DO_NOT_EMIT") {
				t.Fatal("Secret contents leaked")
			}
			if mode == "stuck" && (len(x.r.Inspection.Remaining) != 3 || !strings.Contains(string(b), "example.test/hold")) {
				t.Fatal(string(b))
			}
			if mode != "stuck" && (x.r.Inspection.State != "incomplete" || len(x.r.Inspection.Errors) == 0) {
				t.Fatal(string(b))
			}
			calls, _ := os.ReadFile(os.Getenv("CALLS"))
			if strings.Contains(string(calls), "delete") {
				t.Fatal("inspection mutated resources")
			}
		})
	}
}

func TestRecoveryRefusesIdentityMismatch(t *testing.T) {
	for _, ns := range []string{`{"metadata":{"uid":"uid-1"}}`, strings.ReplaceAll(ownedNS(""), "uid-1", "uid-other"), strings.ReplaceAll(ownedNS(""), "uid-1", "")} {
		x := recoveryTools(t, "stuck")
		t.Setenv("NAMESPACE_JSON", ns)
		if err := x.retryCleanup(); err == nil {
			t.Fatal("accepted mismatch")
		}
		calls, _ := os.ReadFile(os.Getenv("CALLS"))
		if strings.Contains(string(calls), "delete") {
			t.Fatalf("deleted mismatched namespace: %s", calls)
		}
	}
}

func TestRecoveryArguments(t *testing.T) {
	base := []string{"cleanup-retry", "--repository-id", "12", "--pr", "3", "--source-repository", "owner/demo"}
	if _, err := parse(base); err == nil {
		t.Fatal("accepted retry without original UID")
	}
	base = append(base, "--namespace-uid", "uid-1")
	if _, err := parse(base); err != nil {
		t.Fatal(err)
	}
	for _, count := range []string{"0", "11"} {
		if _, err := parse(append(append([]string{}, base...), "--cleanup-attempts", count)); err == nil {
			t.Fatal("unbounded retries")
		}
	}
	base[0] = "cleanup-inspect"
	if _, err := parse(base); err != nil {
		t.Fatal(err)
	}
}

// A fresh runner can resume a previous process's interrupted cleanup with the saved UID.
func TestCleanupRetryAcrossInvocations(t *testing.T) {
	x := recoveryTools(t, "interrupted")
	x.o.cleanupAttempts = 1
	if err := x.retryCleanup(); err == nil {
		t.Fatal("interruption reported success")
	}
	next := &runner{o: x.o, r: result{Namespace: x.r.Namespace}}
	if err := next.retryCleanup(); err != nil {
		t.Fatal(err)
	}
	if next.r.Cleanup != "confirmed_absent" {
		t.Fatal(next.r.Cleanup)
	}
}
