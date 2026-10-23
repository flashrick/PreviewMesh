package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// failingUserWriter makes ignored JSON encoder errors observable to callers.
type failingUserWriter struct{}

func (failingUserWriter) Write([]byte) (int, error) { return 0, errors.New("output write failed") }

func TestUserRMJSONConfirmationStaysOffStdout(t *testing.T) {
	fixture := newUserCLIConfigFixture(t, "preview.test")
	userCLIInstallRMFakes(t, fixture, "current", "closed")

	args := []string{
		"--config", fixture.setup,
		"--kubeconfig", fixture.kubeconfig,
		"owner/demo#3", "--json",
	}
	var out, errOut strings.Builder
	err := runUserRM(args, &out, &errOut, strings.NewReader("n\n"))
	if err == nil || !strings.Contains(err.Error(), "removal cancelled") {
		t.Fatalf("cancelled removal error = %v", err)
	}
	if strings.Contains(out.String(), "Remove preview") {
		t.Fatalf("confirmation prompt contaminated JSON stdout: %q", out.String())
	}
	if !strings.Contains(errOut.String(), "Remove preview pm-r12-pr3? [y/N]") {
		t.Fatalf("confirmation prompt was not sent to stderr: %q", errOut.String())
	}
}

func TestUserRMJSONPropagatesOutputErrors(t *testing.T) {
	fixture := newUserCLIConfigFixture(t, "preview.test")
	userCLIInstallRMFakes(t, fixture, "current", "closed")

	args := []string{
		"--config", fixture.setup,
		"--kubeconfig", fixture.kubeconfig,
		"owner/demo#3", "--json", "--yes",
	}
	var errOut strings.Builder
	err := runUserRM(args, failingUserWriter{}, &errOut, strings.NewReader(""))
	if err == nil || !strings.Contains(err.Error(), "output write failed") {
		t.Fatalf("JSON output write error = %v, want propagated writer error", err)
	}
}

func TestUserKubectlAddsBoundedDeadline(t *testing.T) {
	runtime := newUserRuntime(userCLITestConfig())
	var seen context.Context
	runtime.run = func(ctx context.Context, _ string, _ ...string) ([]byte, error) {
		seen = ctx
		return []byte("{}"), nil
	}

	started := time.Now()
	if _, err := runtime.kubectl(context.Background(), "get", "namespaces"); err != nil {
		t.Fatal(err)
	}
	deadline, ok := seen.Deadline()
	if !ok {
		t.Fatal("kubectl context has no deadline")
	}
	remaining := time.Until(deadline)
	if remaining < 50*time.Second || remaining > 70*time.Second {
		t.Fatalf("kubectl deadline is %s after %s; want approximately one minute", remaining, time.Since(started))
	}
}

func TestUserKubectlRespectsEarlierParentDeadline(t *testing.T) {
	runtime := newUserRuntime(userCLITestConfig())
	var seen context.Context
	runtime.run = func(ctx context.Context, _ string, _ ...string) ([]byte, error) {
		seen = ctx
		return []byte("{}"), nil
	}

	parentDeadline := time.Now().Add(150 * time.Millisecond)
	ctx, cancel := context.WithDeadline(context.Background(), parentDeadline)
	defer cancel()
	if _, err := runtime.kubectl(ctx, "get", "namespaces"); err != nil {
		t.Fatal(err)
	}
	deadline, ok := seen.Deadline()
	if !ok || deadline.After(parentDeadline.Add(10*time.Millisecond)) {
		t.Fatalf("kubectl deadline %v does not respect parent deadline %v", deadline, parentDeadline)
	}
}

func userResourcesWithoutIngress(t *testing.T, image string) []byte {
	t.Helper()
	return userCLITestJSON(t, map[string]any{
		"items": []any{
			map[string]any{
				"kind": "Deployment",
				"spec": map[string]any{
					"replicas": 1,
					"template": map[string]any{
						"spec": map[string]any{
							"containers": []any{map[string]any{"image": image}},
						},
					},
				},
				"status": map[string]any{"readyReplicas": 1},
			},
			map[string]any{
				"kind": "Service",
				"spec": map[string]any{"clusterIP": "10.0.0.10"},
			},
		},
	})
}

func TestUserPreviewDoesNotInventURLWithoutIngress(t *testing.T) {
	var namespace userNamespace
	if err := json.Unmarshal(userCLITestNamespace(t, "pm-r12-pr3", "uid", "12", "3", "owner/demo"), &namespace); err != nil {
		t.Fatal(err)
	}
	runtime := newUserRuntime(userCLITestConfig())
	resources := userResourcesWithoutIngress(t, "ghcr.io/control/previewmesh-c34-r12@sha256:"+strings.Repeat("b", 64))
	runtime.run = func(_ context.Context, _ string, args ...string) ([]byte, error) {
		if strings.Contains(strings.Join(args, " "), "get deployment,service,ingress,pods") {
			return resources, nil
		}
		return nil, errors.New("unexpected kubectl command")
	}

	preview, err := runtime.previewFromNamespace(context.Background(), namespace, &runtime.config.Registrations[0])
	if err != nil {
		t.Fatal(err)
	}
	if preview.URL != "" {
		t.Fatalf("preview URL = %q without an Ingress host", preview.URL)
	}
}

func TestUserPreviewDoesNotInventURLWithHostlessIngress(t *testing.T) {
	var namespace userNamespace
	if err := json.Unmarshal(userCLITestNamespace(t, "pm-r12-pr3", "uid", "12", "3", "owner/demo"), &namespace); err != nil {
		t.Fatal(err)
	}
	runtime := newUserRuntime(userCLITestConfig())
	resources := userCLITestResources(t, "ghcr.io/control/previewmesh-c34-r12@sha256:"+strings.Repeat("b", 64), "", 1)
	runtime.run = func(_ context.Context, _ string, args ...string) ([]byte, error) {
		if strings.Contains(strings.Join(args, " "), "get deployment,service,ingress,pods") {
			return resources, nil
		}
		return nil, errors.New("unexpected kubectl command")
	}

	preview, err := runtime.previewFromNamespace(context.Background(), namespace, &runtime.config.Registrations[0])
	if err != nil {
		t.Fatal(err)
	}
	if preview.URL != "" {
		t.Fatalf("preview URL = %q for an Ingress without a host", preview.URL)
	}
}

func TestUserPreviewRejectsAmbiguousRuntimeResources(t *testing.T) {
	var namespace userNamespace
	if err := json.Unmarshal(userCLITestNamespace(t, "pm-r12-pr3", "uid", "12", "3", "owner/demo"), &namespace); err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		name  string
		index int
		kind  string
	}{
		{name: "deployment", index: 0, kind: "Deployment"},
		{name: "service", index: 1, kind: "Service"},
		{name: "ingress", index: 2, kind: "Ingress"},
	} {
		t.Run(test.name, func(t *testing.T) {
			base := userCLITestResources(t, "ghcr.io/control/previewmesh-c34-r12@sha256:"+strings.Repeat("b", 64), "pm-r12-pr3.preview.test", 1)
			var payload map[string]any
			if err := json.Unmarshal(base, &payload); err != nil {
				t.Fatal(err)
			}
			items, ok := payload["items"].([]any)
			if !ok || len(items) <= test.index {
				t.Fatalf("fixture items = %#v", payload["items"])
			}
			payload["items"] = append(items, items[test.index])
			resources, err := json.Marshal(payload)
			if err != nil {
				t.Fatal(err)
			}

			runtime := newUserRuntime(userCLITestConfig())
			runtime.run = func(_ context.Context, _ string, args ...string) ([]byte, error) {
				if strings.Contains(strings.Join(args, " "), "get deployment,service,ingress,pods") {
					return resources, nil
				}
				return nil, errors.New("unexpected kubectl command")
			}
			preview, err := runtime.previewFromNamespace(context.Background(), namespace, &runtime.config.Registrations[0])
			if err == nil || !strings.Contains(err.Error(), "ambiguous runtime resources") || !strings.Contains(err.Error(), test.kind) {
				t.Fatalf("ambiguous %s result = preview=%+v err=%v", test.kind, preview, err)
			}
			if preview.RuntimeState != "unknown" {
				t.Fatalf("ambiguous %s runtime state = %q, want unknown", test.kind, preview.RuntimeState)
			}
		})
	}
}

func TestUserHealthProbeDoesNotFollowRedirects(t *testing.T) {
	wrongEndpointRequests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/health" {
			http.Redirect(w, r, "/wrong-endpoint", http.StatusFound)
			return
		}
		if r.URL.Path == "/wrong-endpoint" {
			wrongEndpointRequests++
			_, _ = w.Write([]byte(`{"status":"ok","commit_sha":"` + userCLITestSHA('d') + `"}`))
			return
		}
		http.NotFound(w, r)
	}))
	defer server.Close()

	runtime := newUserRuntime(userConfig{})
	health := runtime.probeHealth(context.Background(), server.URL, time.Second)
	if health.Healthy {
		t.Fatalf("redirected health response was reported healthy: %+v", health)
	}
	if health.HTTPStatus != http.StatusFound {
		t.Fatalf("health status = %d, want %d for redirect", health.HTTPStatus, http.StatusFound)
	}
	if wrongEndpointRequests != 0 {
		t.Fatalf("health probe followed redirect to wrong endpoint %d time(s)", wrongEndpointRequests)
	}
}

func TestUserHealthProbeReportsPlainTextNon200(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/health" {
			t.Errorf("health probe requested %s, want /health", r.URL.Path)
		}
		w.WriteHeader(http.StatusBadGateway)
		_, _ = w.Write([]byte("upstream unavailable"))
	}))
	defer server.Close()

	health := newUserRuntime(userConfig{}).probeHealth(context.Background(), server.URL, time.Second)
	if health.Healthy {
		t.Fatalf("plain-text non-200 response was reported healthy: %+v", health)
	}
	if health.HTTPStatus != http.StatusBadGateway {
		t.Fatalf("health status = %d, want %d", health.HTTPStatus, http.StatusBadGateway)
	}
	if !strings.Contains(health.Error, "HTTP 502") {
		t.Fatalf("plain-text non-200 error = %q, want HTTP status", health.Error)
	}
}

func TestPrintHealthSanitizesCommitSHA(t *testing.T) {
	var out strings.Builder
	printHealth(&out, userHealth{URL: "http://preview.test", CommitSHA: "sha\nINJECT"})
	if strings.Contains(out.String(), "sha\nINJECT") {
		t.Fatalf("commit SHA was written with terminal control characters: %q", out.String())
	}
	if !strings.Contains(out.String(), "sha?INJECT") {
		t.Fatalf("sanitized commit SHA missing from output: %q", out.String())
	}
}
