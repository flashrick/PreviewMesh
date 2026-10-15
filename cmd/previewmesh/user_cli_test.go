package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func userCLITestSHA(ch byte) string {
	return strings.Repeat(string(ch), 40)
}

func userCLITestConfig() userConfig {
	return userConfig{
		Control:      "flashrick/control",
		ControlID:    "34",
		Kubeconfig:   "/tmp/previewmesh-test-kubeconfig",
		DomainSuffix: "preview.test",
		Registrations: []userRegistration{
			{ID: "12", Source: "owner/demo", Port: 8080},
			{ID: "13", Source: "other/repo", Port: 8081},
		},
	}
}

func userCLITestJSON(t *testing.T, value any) []byte {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func userCLITestNamespace(t *testing.T, name, uid, repoID, pr, source string) []byte {
	t.Helper()
	return userCLITestJSON(t, map[string]any{
		"metadata": map[string]any{
			"name": name,
			"uid":  uid,
			"labels": map[string]string{
				domain + "managed-by":    "previewmesh",
				domain + "repository-id": repoID,
				domain + "pr-number":     pr,
			},
			"annotations": map[string]string{
				domain + "source-repository": source,
				domain + "verified-sha":      userCLITestSHA('a'),
			},
		},
	})
}

func userCLITestResources(t *testing.T, image, host string, ready int) []byte {
	t.Helper()
	return userCLITestJSON(t, map[string]any{
		"items": []any{
			map[string]any{
				"kind":     "Deployment",
				"metadata": map[string]any{"name": "preview"},
				"spec": map[string]any{
					"replicas": ready,
					"template": map[string]any{
						"spec": map[string]any{
							"containers": []any{map[string]any{"image": image}},
						},
					},
				},
				"status": map[string]any{"readyReplicas": ready},
			},
			map[string]any{
				"kind": "Service",
				"spec": map[string]any{"clusterIP": "10.0.0.10"},
			},
			map[string]any{
				"kind": "Ingress",
				"spec": map[string]any{
					"rules": []any{map[string]any{"host": host}},
				},
			},
			map[string]any{
				"kind": "Pod",
				"status": map[string]any{
					"phase": "Running",
					"conditions": []any{map[string]any{
						"type": "Ready", "status": "True",
					}},
				},
			},
		},
	})
}

func userCLITestRuntime(t *testing.T) *userRuntime {
	t.Helper()
	runtime := newUserRuntime(userCLITestConfig())
	runtime.run = func(_ context.Context, command string, args ...string) ([]byte, error) {
		if command == "kubectl" && strings.Contains(strings.Join(args, " "), "get namespace") {
			// Target resolution only needs the fake command to report an absent Namespace.
			return []byte{}, nil
		}
		return nil, fmt.Errorf("unexpected command %q %q", command, args)
	}
	return runtime
}

func TestUserCLIHelp(t *testing.T) {
	tests := []struct {
		name string
		args []string
		want []string
	}{
		{name: "default", want: []string{"User commands:", "previewmesh ps", "previewmesh rm"}},
		{name: "rm", args: []string{"help", "rm"}, want: []string{"Usage: previewmesh rm", "open PR requires --force"}},
		{name: "health", args: []string{"help", "health"}, want: []string{"Usage: previewmesh health", "GET request"}},
		{name: "flag help", args: []string{"ps", "--help"}, want: []string{"Usage: previewmesh ps", "--kubeconfig PATH"}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			var out, errOut strings.Builder
			if err := runUserCLI(test.args, &out, &errOut, strings.NewReader("")); err != nil {
				t.Fatalf("runUserCLI returned error: %v", err)
			}
			for _, want := range test.want {
				if !strings.Contains(out.String(), want) {
					t.Errorf("help output missing %q:\n%s", want, out.String())
				}
			}
			if errOut.Len() != 0 {
				t.Fatalf("unexpected stderr: %s", errOut.String())
			}
		})
	}
}

func TestUserArgumentAndTargetParsing(t *testing.T) {
	flags, positionals, err := normalizeUserArgs([]string{
		"--json", "owner/demo#3", "--config", "setup.ini", "--kubeconfig=/tmp/runner", "--", "--literal",
	}, map[string]bool{"config": true, "kubeconfig": true})
	if err != nil {
		t.Fatal(err)
	}
	if got, want := strings.Join(flags, "|"), "--json|--config|setup.ini|--kubeconfig=/tmp/runner"; got != want {
		t.Fatalf("flags = %q, want %q", got, want)
	}
	if got, want := strings.Join(positionals, "|"), "owner/demo#3|--literal"; got != want {
		t.Fatalf("positionals = %q, want %q", got, want)
	}
	if _, _, err := normalizeUserArgs([]string{"--config"}, map[string]bool{"config": true}); err == nil || userCLIExitCode(err) != 2 {
		t.Fatalf("missing flag value error = %v", err)
	}

	runtime := userCLITestRuntime(t)
	tests := []struct {
		name       string
		raw        string
		wantNS     string
		wantSource string
		wantErr    string
	}{
		{name: "source and PR", raw: "owner/demo#3", wantNS: "pm-r12-pr3", wantSource: "owner/demo"},
		{name: "source basename and PR", raw: "demo#4", wantNS: "pm-r12-pr4", wantSource: "owner/demo"},
		{name: "full preview name", raw: "pm-r12-pr5", wantNS: "pm-r12-pr5", wantSource: "owner/demo"},
		{name: "ambiguous PR", raw: "3", wantErr: "ambiguous"},
		{name: "invalid PR", raw: "owner/demo#0", wantErr: "positive decimal"},
		{name: "invalid whitespace", raw: "owner/demo#3\n", wantErr: "TARGET must"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, namespace, err := runtime.resolveTarget(context.Background(), test.raw)
			if test.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), test.wantErr) {
					t.Fatalf("resolveTarget error = %v, want substring %q", err, test.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if namespace != nil {
				t.Fatalf("resolveTarget returned an unexpected namespace: %+v", namespace)
			}
			if got.Namespace != test.wantNS || got.Source != test.wantSource {
				t.Fatalf("target = %+v, want namespace %q source %q", got, test.wantNS, test.wantSource)
			}
		})
	}
}

type userCLIConfigFixture struct {
	root       string
	setup      string
	kubeconfig string
	home       string
	calls      string
	controlDir string
}

func newUserCLIConfigFixture(t *testing.T, domainSuffix string) userCLIConfigFixture {
	t.Helper()
	root := t.TempDir()
	controlDir := filepath.Join(root, "control")
	if err := os.MkdirAll(filepath.Join(controlDir, "config"), 0700); err != nil {
		t.Fatal(err)
	}
	setup := filepath.Join(root, "setup.ini")
	network := "domain_suffix = " + domainSuffix + "\n"
	if domainSuffix == "auto" {
		network += "lan_ip = 192.168.1.20\n"
	}
	setupData := "[project]\ncontrol_repository = flashrick/control\ncontrol_directory = control\n\n[network]\n" + network
	if err := os.WriteFile(setup, []byte(setupData), 0600); err != nil {
		t.Fatal(err)
	}
	registry := `[{"repository_id":"12","source_repository":"owner/demo","port":8080}]`
	if err := os.WriteFile(filepath.Join(controlDir, "config", "repositories.json"), []byte(registry), 0600); err != nil {
		t.Fatal(err)
	}
	home := filepath.Join(root, "home")
	stateDir := filepath.Join(home, ".local", "share", "previewmesh")
	if err := os.MkdirAll(stateDir, 0700); err != nil {
		t.Fatal(err)
	}
	state := `{"control":"flashrick/control","control_id":34}`
	if err := os.WriteFile(filepath.Join(stateDir, "install-state.json"), []byte(state), 0600); err != nil {
		t.Fatal(err)
	}
	calls := filepath.Join(root, "kubectl.calls")
	t.Setenv("HOME", home)
	t.Setenv(userDefaultConfigEnv, "")
	t.Setenv(userKubeconfigEnv, "")
	return userCLIConfigFixture{
		root: root, setup: setup, kubeconfig: filepath.Join(root, "runner.yaml"),
		home: home, calls: calls, controlDir: controlDir,
	}
}

func TestLoadUserConfigReadsSetupAndRegistry(t *testing.T) {
	fixture := newUserCLIConfigFixture(t, "auto")
	config, err := loadUserConfig(fixture.setup, "")
	if err != nil {
		t.Fatal(err)
	}
	wantControlDir, err := filepath.Abs(fixture.controlDir)
	if err != nil {
		t.Fatal(err)
	}
	if config.Path != fixture.setup || config.Control != "flashrick/control" || config.ControlDirectory != wantControlDir {
		t.Fatalf("config project fields = %+v", config)
	}
	if config.ControlID != "34" || config.DomainSuffix != "192.168.1.20.sslip.io" {
		t.Fatalf("config derived fields = %+v", config)
	}
	if config.RegistryPath != filepath.Join(wantControlDir, "config", "repositories.json") {
		t.Fatalf("registry path = %q", config.RegistryPath)
	}
	if config.Kubeconfig != filepath.Join(wantControlDir, "config", "previewmesh-runner.yaml") {
		t.Fatalf("default kubeconfig = %q", config.Kubeconfig)
	}
	if len(config.Registrations) != 1 || config.Registrations[0] != (userRegistration{ID: "12", Source: "owner/demo", Port: 8080}) {
		t.Fatalf("registrations = %+v", config.Registrations)
	}

	override := filepath.Join(fixture.root, "custom-kubeconfig")
	config, err = loadUserConfig(fixture.setup, override)
	if err != nil {
		t.Fatal(err)
	}
	if config.Kubeconfig != override {
		t.Fatalf("explicit kubeconfig = %q, want %q", config.Kubeconfig, override)
	}
}

func TestLoadUserConfigRejectsEmptyRegistry(t *testing.T) {
	fixture := newUserCLIConfigFixture(t, "preview.test")
	registryPath := filepath.Join(fixture.controlDir, "config", "repositories.json")
	if err := os.WriteFile(registryPath, []byte(`[]`), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := loadUserConfig(fixture.setup, fixture.kubeconfig); err == nil || !strings.Contains(err.Error(), "invalid or empty") {
		t.Fatalf("empty registry error = %v", err)
	}
}

func userCLIWriteExecutable(t *testing.T, path, contents string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(contents), 0700); err != nil {
		t.Fatal(err)
	}
}

func TestUserHealthProbe(t *testing.T) {
	sha := userCLITestSHA('d')
	tests := []struct {
		name       string
		statusCode int
		body       string
		want       bool
		wantError  string
	}{
		{name: "healthy", statusCode: http.StatusOK, body: `{"status":"ok","commit_sha":"` + sha + `"}`, want: true},
		{name: "wrong status", statusCode: http.StatusServiceUnavailable, body: `{"status":"ok","commit_sha":"` + sha + `"}`, wantError: "status ok"},
		{name: "malformed JSON", statusCode: http.StatusOK, body: "not-json", wantError: "not valid JSON"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			var path string
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				path = r.URL.Path
				w.WriteHeader(test.statusCode)
				_, _ = w.Write([]byte(test.body))
			}))
			defer server.Close()
			runtime := &userRuntime{httpClient: server.Client()}
			got := runtime.probeHealth(context.Background(), server.URL+"/", time.Second)
			if got.Healthy != test.want || got.HTTPStatus != test.statusCode || path != "/health" {
				t.Fatalf("health = %+v, path = %q", got, path)
			}
			if test.wantError != "" && !strings.Contains(got.Error, test.wantError) {
				t.Fatalf("health error = %q, want substring %q", got.Error, test.wantError)
			}
		})
	}

	invalid := (&userRuntime{httpClient: http.DefaultClient}).probeHealth(context.Background(), "ftp://preview.test", time.Second)
	if invalid.Healthy || invalid.Error != "preview URL is invalid" {
		t.Fatalf("invalid URL health = %+v", invalid)
	}
}

func userCLIInstallRMFakes(t *testing.T, fixture userCLIConfigFixture, mode, prState string) {
	t.Helper()
	bin := filepath.Join(fixture.root, "bin")
	if err := os.MkdirAll(bin, 0700); err != nil {
		t.Fatal(err)
	}
	currentNamespace := userCLITestNamespace(t, "pm-r12-pr3", "uid-current", "12", "3", "owner/demo")
	foreignNamespace := userCLITestNamespace(t, "pm-r12-pr3", "uid-foreign", "12", "3", "owner/demo")
	currentResources := userCLITestResources(t, "ghcr.io/control/previewmesh-c34-r12@sha256:"+strings.Repeat("b", 64), "pm-r12-pr3.preview.test", 1)
	foreignResources := userCLITestResources(t, "ghcr.io/other/previewmesh-c99-r12@sha256:"+strings.Repeat("c", 64), "pm-r12-pr3.preview.test", 1)
	kubectl := fmt.Sprintf(`#!/bin/sh
printf '%%s\n' "$*" >> "$PREVIEWMESH_TEST_CALLS"
case "$*" in
  *"get namespace pm-r12-pr3 --ignore-not-found -o json"*)
    case "$PREVIEWMESH_TEST_KUBE_MODE" in
      missing) exit 0 ;;
      foreign) printf '%%s' '%s' ;;
      current) printf '%%s' '%s' ;;
      *) exit 1 ;;
    esac ;;
  *"get deployment,service,ingress,pods -n pm-r12-pr3 -o json"*)
    case "$PREVIEWMESH_TEST_KUBE_MODE" in
      foreign) printf '%%s' '%s' ;;
      current) printf '%%s' '%s' ;;
      *) exit 1 ;;
    esac ;;
  *) exit 1 ;;
esac
`, foreignNamespace, currentNamespace, foreignResources, currentResources)
	userCLIWriteExecutable(t, filepath.Join(bin, "kubectl"), kubectl)
	gh := fmt.Sprintf(`#!/bin/sh
case "$4" in
  repos/owner/demo/pulls/3)
    case "$PREVIEWMESH_TEST_PR_STATE" in
      open) printf '%%s' '{"number":3,"state":"open","merged":false,"head":{"sha":"%s"}}' ;;
      pending) printf '%%s' '{"number":3,"state":"pending","merged":false,"head":{"sha":"%s"}}' ;;
      *) printf '%%s' '{"number":3,"state":"closed","merged":false,"head":{"sha":"%s"}}' ;;
    esac ;;
  *) exit 1 ;;
esac
`, userCLITestSHA('a'), userCLITestSHA('a'), userCLITestSHA('a'))
	userCLIWriteExecutable(t, filepath.Join(bin, "gh"), gh)
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	t.Setenv("PREVIEWMESH_TEST_CALLS", fixture.calls)
	t.Setenv("PREVIEWMESH_TEST_KUBE_MODE", mode)
	t.Setenv("PREVIEWMESH_TEST_PR_STATE", prState)
}

func userCLITestRM(t *testing.T, mode, prState string, args []string, input string) (string, error, string) {
	t.Helper()
	fixture := newUserCLIConfigFixture(t, "preview.test")
	userCLIInstallRMFakes(t, fixture, mode, prState)
	args = append([]string{"--config", fixture.setup, "--kubeconfig", fixture.kubeconfig}, args...)
	var out, errOut strings.Builder
	err := runUserRM(args, &out, &errOut, strings.NewReader(input))
	calls, readErr := os.ReadFile(fixture.calls)
	if readErr != nil && !os.IsNotExist(readErr) {
		t.Fatal(readErr)
	}
	return out.String(), err, string(calls)
}

func TestUserRMSafetyPolicies(t *testing.T) {
	t.Run("open PR requires force", func(t *testing.T) {
		out, err, calls := userCLITestRM(t, "current", "open", []string{"owner/demo#3", "--yes"}, "")
		if err == nil || userCLIExitCode(err) != 2 || !strings.Contains(err.Error(), "PR 3 is still open") {
			t.Fatalf("open PR removal error = %v, output = %s", err, out)
		}
		if strings.Contains(calls, "delete") {
			t.Fatalf("open PR removal issued delete: %s", calls)
		}
	})

	t.Run("unknown PR state is refused", func(t *testing.T) {
		out, err, calls := userCLITestRM(t, "current", "pending", []string{"owner/demo#3", "--yes"}, "")
		if err == nil || userCLIExitCode(err) == 0 || !strings.Contains(err.Error(), `unsupported PR state "pending"`) {
			t.Fatalf("unknown PR state removal error = %v, output = %s", err, out)
		}
		if strings.Contains(calls, "delete") || strings.Contains(calls, "apply") {
			t.Fatalf("unknown PR state removal issued mutation: %s", calls)
		}
	})

	t.Run("foreign control is refused", func(t *testing.T) {
		out, err, calls := userCLITestRM(t, "foreign", "closed", []string{"owner/demo#3", "--yes"}, "")
		if err == nil || userCLIExitCode(err) != 2 || !strings.Contains(err.Error(), "control ownership is foreign") {
			t.Fatalf("foreign removal error = %v, output = %s", err, out)
		}
		if strings.Contains(calls, "delete") || strings.Contains(calls, "apply") {
			t.Fatalf("foreign removal issued mutation: %s", calls)
		}
	})

	t.Run("confirmation can cancel", func(t *testing.T) {
		out, err, calls := userCLITestRM(t, "current", "closed", []string{"owner/demo#3"}, "n\n")
		if err == nil || userCLIExitCode(err) != 2 || !strings.Contains(err.Error(), "removal cancelled") {
			t.Fatalf("cancelled removal error = %v, output = %s", err, out)
		}
		if !strings.Contains(out, "Remove preview pm-r12-pr3? [y/N]") {
			t.Fatalf("confirmation prompt missing: %s", out)
		}
		if strings.Contains(calls, "delete") {
			t.Fatalf("cancelled removal issued delete: %s", calls)
		}
	})

	t.Run("already absent is idempotent", func(t *testing.T) {
		out, err, calls := userCLITestRM(t, "missing", "closed", []string{"owner/demo#3", "--yes", "--json"}, "")
		if err != nil {
			t.Fatalf("absent removal returned error: %v", err)
		}
		var result map[string]string
		if err := json.Unmarshal([]byte(out), &result); err != nil {
			t.Fatalf("idempotent result is not JSON: %v (%s)", err, out)
		}
		if result["state"] != "removed" || result["message"] != "already absent" || result["target"] != "pm-r12-pr3" {
			t.Fatalf("idempotent result = %+v", result)
		}
		if strings.Contains(calls, "delete") || strings.Contains(calls, "apply") {
			t.Fatalf("absent removal issued mutation: %s", calls)
		}
	})
}

func TestUserRMConfirmationAnswers(t *testing.T) {
	for _, test := range []struct {
		answer string
		want   bool
	}{
		{answer: "y\n", want: true},
		{answer: "YES\n", want: true},
		{answer: "n\n", want: false},
	} {
		var out strings.Builder
		got, err := confirmRemoval(strings.NewReader(test.answer), &out, "pm-r12-pr3")
		if err != nil || got != test.want {
			t.Fatalf("answer %q = %t, %v; want %t", test.answer, got, err, test.want)
		}
	}
	if _, err := confirmRemoval(nil, &strings.Builder{}, "pm-r12-pr3"); err == nil || !strings.Contains(err.Error(), "use --yes") {
		t.Fatal("missing confirmation input was accepted")
	}
}

func TestPrintPreviewTableEmpty(t *testing.T) {
	var out strings.Builder
	printPreviewTable(&out, nil)
	if !strings.Contains(out.String(), "(no previews)") {
		t.Fatalf("empty preview table missing marker: %s", out.String())
	}
}

func TestUserPreviewJSONStableFields(t *testing.T) {
	sha := userCLITestSHA('e')
	data, err := json.Marshal(userPreview{
		RuntimeState:   "running",
		LifecycleState: "ready",
		ReadyReplicas:  1,
		URL:            "http://pm-r12-pr3.preview.test:18080",
		VerifiedSHA:    sha,
	})
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		t.Fatal(err)
	}
	for _, field := range []string{"runtime_state", "lifecycle_state", "ready_replicas", "url", "verified_sha"} {
		if _, ok := fields[field]; !ok {
			t.Errorf("preview JSON missing stable field %q: %s", field, data)
		}
	}
}
