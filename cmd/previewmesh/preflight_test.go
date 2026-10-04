package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

type preflightSource struct {
	dir string
	sha string
}

func runGit(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", append([]string{"-C", dir}, args...)...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v failed: %v\n%s", args, err, out)
	}
	return strings.TrimSpace(string(out))
}

func makePreflightSource(t *testing.T, dockerfile string, files map[string]string) preflightSource {
	t.Helper()
	dir := t.TempDir()
	runGit(t, dir, "init", "-q")
	runGit(t, dir, "config", "user.email", "previewmesh-test@example.invalid")
	runGit(t, dir, "config", "user.name", "PreviewMesh test")
	if dockerfile != "" {
		if err := os.WriteFile(filepath.Join(dir, "Dockerfile"), []byte(dockerfile), 0600); err != nil {
			t.Fatal(err)
		}
	}
	for name, content := range files {
		path := filepath.Join(dir, name)
		if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(content), 0600); err != nil {
			t.Fatal(err)
		}
	}
	runGit(t, dir, "add", "--all")
	runGit(t, dir, "commit", "-qm", "fixture")
	return preflightSource{dir: dir, sha: runGit(t, dir, "rev-parse", "HEAD")}
}

func completeSource(t *testing.T, extra string) preflightSource {
	t.Helper()
	return makePreflightSource(t, "FROM scratch\nEXPOSE 8080\nUSER 65532:65532\n", map[string]string{
		"main.go": "package main\n// listen 0.0.0.0; GET /health; PREVIEW_COMMIT_SHA; commit_sha\n" + extra + "\n",
	})
}

func findingByID(report *preflightReport, id string) (preflightFinding, bool) {
	for _, finding := range report.Findings {
		if finding.Check == id {
			return finding, true
		}
	}
	return preflightFinding{}, false
}

func requireFinding(t *testing.T, report *preflightReport, id, severity string) preflightFinding {
	t.Helper()
	finding, ok := findingByID(report, id)
	if !ok || finding.Severity != severity || finding.Fix == "" {
		t.Fatalf("missing %s %s finding with fix: %#v", severity, id, report.Findings)
	}
	return finding
}

func TestInspectApplicationReportsVersionConfigAndStaticWarnings(t *testing.T) {
	source := makePreflightSource(t, "FROM scratch\nEXPOSE 9000\n", map[string]string{"main.go": "package main\n"})
	report, err := inspectApplication(source.dir, source.sha, 8080, time.Second)
	if err != nil {
		t.Fatalf("clean source was blocked: %v", err)
	}
	if report.SHA != source.sha || report.Port != 8080 || report.Static != "passed_with_warnings" || report.Container != "not_requested" || report.Deployment != "not_attempted" {
		t.Fatalf("unexpected static report: %#v", report)
	}
	for _, check := range []string{"port", "non_root", "listen_address", "health", "commit_sha", "health_payload", "static_limits"} {
		requireFinding(t, report, check, "warning")
	}
	if len(report.Fingerprint) != 64 {
		t.Fatalf("configuration fingerprint is not a SHA-256 hex value: %q", report.Fingerprint)
	}
	if _, err := time.Parse(time.RFC3339, report.CheckedAt); err != nil {
		t.Fatalf("invalid checked_at timestamp %q: %v", report.CheckedAt, err)
	}
	other, err := inspectApplication(source.dir, source.sha, 8081, time.Second)
	if err != nil {
		t.Fatalf("port-only configuration change was blocked: %v", err)
	}
	if other.Fingerprint == report.Fingerprint {
		t.Fatal("configuration fingerprint did not change when the inspected port changed")
	}
}

func TestInspectApplicationAcceptsCompleteContract(t *testing.T) {
	source := makePreflightSource(t, "FROM scratch\nEXPOSE 8080\nUSER 65532:65532\n# 0.0.0.0 /health PREVIEW_COMMIT_SHA commit_sha\n", map[string]string{"main.go": "package main\n"})
	report, err := inspectApplication(source.dir, source.sha, 8080, time.Second)
	if err != nil {
		t.Fatalf("complete source was blocked: %v", err)
	}
	if report.Static != "passed_with_warnings" {
		t.Fatalf("static result should disclose its limits: %q", report.Static)
	}
	if len(report.Findings) != 1 {
		t.Fatalf("complete source should only have the static-limit warning: %#v", report.Findings)
	}
	requireFinding(t, report, "static_limits", "warning")
}

func TestInspectApplicationBlocksDirtyIgnoredRevisionAndDockerfileProblems(t *testing.T) {
	tests := []struct {
		name   string
		make   func(*testing.T) preflightSource
		mutate func(*testing.T, preflightSource)
		sha    string
		check  string
	}{
		{
			name: "dirty checkout",
			make: func(t *testing.T) preflightSource { return completeSource(t, "") },
			mutate: func(t *testing.T, source preflightSource) {
				if err := os.WriteFile(filepath.Join(source.dir, "main.go"), []byte("changed\n"), 0600); err != nil {
					t.Fatal(err)
				}
			},
			check: "clean_checkout",
		},
		{
			name: "ignored file",
			make: func(t *testing.T) preflightSource {
				return makePreflightSource(t, "FROM scratch\nEXPOSE 8080\nUSER 65532:65532\n", map[string]string{
					".gitignore": "ignored.txt\n",
					"main.go":    "package main\n",
				})
			},
			mutate: func(t *testing.T, source preflightSource) {
				if err := os.WriteFile(filepath.Join(source.dir, "ignored.txt"), []byte("untracked secret\n"), 0600); err != nil {
					t.Fatal(err)
				}
			},
			check: "clean_checkout",
		},
		{
			name:  "SHA mismatch",
			make:  func(t *testing.T) preflightSource { return completeSource(t, "") },
			sha:   strings.Repeat("f", 40),
			check: "revision",
		},
		{
			name: "missing Dockerfile",
			make: func(t *testing.T) preflightSource {
				return makePreflightSource(t, "", map[string]string{"main.go": "package main\n"})
			},
			check: "dockerfile",
		},
		{
			name: "symlink Dockerfile",
			make: func(t *testing.T) preflightSource {
				source := makePreflightSource(t, "", map[string]string{"Containerfile": "FROM scratch\nEXPOSE 8080\n"})
				if err := os.Symlink("Containerfile", filepath.Join(source.dir, "Dockerfile")); err != nil {
					t.Fatal(err)
				}
				runGit(t, source.dir, "add", "Dockerfile")
				runGit(t, source.dir, "commit", "-qm", "symlink")
				source.sha = runGit(t, source.dir, "rev-parse", "HEAD")
				return source
			},
			check: "dockerfile",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			source := tc.make(t)
			if tc.mutate != nil {
				tc.mutate(t, source)
			}
			expected := tc.sha
			if expected == "" {
				expected = source.sha
			}
			report, err := inspectApplication(source.dir, expected, 8080, time.Second)
			if err == nil {
				t.Fatal("invalid source unexpectedly passed preflight")
			}
			if report.Static != "failed" {
				t.Fatalf("blocked source did not set static=failed: %#v", report)
			}
			requireFinding(t, report, tc.check, "blocker")
		})
	}
}

func writeFakeDocker(t *testing.T, port string) (dir, log string) {
	t.Helper()
	dir = t.TempDir()
	log = filepath.Join(dir, "docker.log")
	script := `#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
if [ "$1" = port ]; then
  printf '127.0.0.1:%s\n' "$FAKE_DOCKER_PORT"
fi
if [ "$1" = run ] && [ "${FAKE_DOCKER_FAIL_RUN:-}" = 1 ]; then
  exit 1
fi
if [ "$1" = run ] && [ -n "${FAKE_DOCKER_MUTATE_DIR:-}" ]; then
  printf 'changed during container check\n' > "$FAKE_DOCKER_MUTATE_DIR/main.go"
fi
if [ "${FAKE_DOCKER_FAIL_CLEANUP:-}" = 1 ] && { [ "$1" = rm ] || { [ "$1" = image ] && [ "$2" = rm ]; }; }; then
  exit 1
fi
`
	if err := os.WriteFile(filepath.Join(dir, "docker"), []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("FAKE_DOCKER_LOG", log)
	t.Setenv("FAKE_DOCKER_PORT", port)
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	return dir, log
}

func readDockerLog(t *testing.T, log string) string {
	t.Helper()
	b, err := os.ReadFile(log)
	if err != nil {
		if os.IsNotExist(err) {
			return ""
		}
		t.Fatal(err)
	}
	return string(b)
}

func runPreflightReport(t *testing.T, source preflightSource, extra ...string) (preflightReport, string, error) {
	t.Helper()
	args := []string{"--source-dir", source.dir, "--port", "8080", "--timeout", "5s"}
	args = append(args, extra...)
	var output bytes.Buffer
	err := runPreflight(args, &output)
	var report preflightReport
	if decodeErr := json.Unmarshal(output.Bytes(), &report); decodeErr != nil {
		t.Fatalf("preflight did not emit JSON: %v\n%s", decodeErr, output.String())
	}
	return report, output.String(), err
}

func TestPreflightDefaultIsStaticAndDoesNotEchoCredentialsOrRunDocker(t *testing.T) {
	secret := "previewmesh-test-secret-value"
	source := completeSource(t, fmt.Sprintf("const credential = %q", secret))
	_, log := writeFakeDocker(t, "1")
	report, output, err := runPreflightReport(t, source)
	if err != nil {
		t.Fatalf("static preflight failed: %v", err)
	}
	if report.Container != "not_requested" || report.Deployment != "not_attempted" {
		t.Fatalf("default preflight implies runtime/deployment work: %#v", report)
	}
	if strings.Contains(output, secret) || strings.Contains(errString(err), secret) {
		t.Fatalf("preflight echoed a source credential: %s", output)
	}
	if dockerLog := readDockerLog(t, log); dockerLog != "" {
		t.Fatalf("default preflight invoked Docker: %s", dockerLog)
	}
}

func errString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

func TestContainerPreflightPassesAndCleansTemporaryResources(t *testing.T) {
	source := completeSource(t, "")
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		if r.URL.Path != "/health" {
			t.Errorf("health request path = %s", r.URL.Path)
		}
		fmt.Fprintf(w, `{"status":"ok","commit_sha":%q}`, source.sha)
	}))
	defer server.Close()
	_, port, err := net.SplitHostPort(strings.TrimPrefix(server.URL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	_, log := writeFakeDocker(t, port)
	report, _, err := runPreflightReport(t, source, "--container-check", "--http-timeout", "2s")
	if err != nil {
		t.Fatalf("container preflight failed: %v", err)
	}
	if report.Container != "passed" || report.Deployment != "not_attempted" || requests.Load() == 0 {
		t.Fatalf("unexpected successful container report: %#v (requests=%d)", report, requests.Load())
	}
	if !strings.Contains(report.ContainerConfiguration, "linux/amd64") || !strings.Contains(report.ContainerConfiguration, "memory=512m") {
		t.Fatalf("container configuration was not recorded: %q", report.ContainerConfiguration)
	}
	for _, marker := range []string{"cpus=1", "pids=128", "operation-timeout=5s", "health-timeout=2s"} {
		if !strings.Contains(report.ContainerConfiguration, marker) {
			t.Fatalf("container configuration marker %q missing: %q", marker, report.ContainerConfiguration)
		}
	}
	staticReport, err := inspectApplication(source.dir, source.sha, 8080, time.Second)
	if err != nil || staticReport.Fingerprint == report.Fingerprint {
		t.Fatalf("container configuration did not affect the fingerprint: static=%#v runtime=%#v", staticReport, report)
	}
	dockerLog := readDockerLog(t, log)
	for _, call := range []string{"build --platform linux/amd64", "run --platform linux/amd64", "port ", "rm -f ", "image rm -f "} {
		if !strings.Contains(dockerLog, call) {
			t.Fatalf("container cleanup/build call %q missing from log:\n%s", call, dockerLog)
		}
	}
}

func TestContainerPreflightFailureStillCleansTemporaryResources(t *testing.T) {
	source := completeSource(t, "")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, `{"status":"ok","commit_sha":%q}`, strings.Repeat("b", 40))
	}))
	defer server.Close()
	_, port, err := net.SplitHostPort(strings.TrimPrefix(server.URL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	_, log := writeFakeDocker(t, port)
	report, output, err := runPreflightReport(t, source, "--container-check", "--http-timeout", "50ms")
	if err == nil {
		t.Fatal("mismatched health revision unexpectedly passed")
	}
	if report.Container != "failed" || report.Static != "passed_with_warnings" {
		t.Fatalf("unexpected failed container report: %#v", report)
	}
	requireFinding(t, &report, "container", "blocker")
	if strings.Contains(output, "b"+strings.Repeat("b", 39)) {
		t.Fatal("failed container report exposed the served revision body")
	}
	dockerLog := readDockerLog(t, log)
	for _, call := range []string{"build --platform linux/amd64", "run --platform linux/amd64", "port ", "rm -f ", "image rm -f "} {
		if !strings.Contains(dockerLog, call) {
			t.Fatalf("failed container cleanup call %q missing from log:\n%s", call, dockerLog)
		}
	}
}

func TestContainerPreflightStartupFailureStillCleansResources(t *testing.T) {
	source := completeSource(t, "")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("health endpoint was called after container startup failed")
	}))
	defer server.Close()
	_, port, err := net.SplitHostPort(strings.TrimPrefix(server.URL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	_, log := writeFakeDocker(t, port)
	t.Setenv("FAKE_DOCKER_FAIL_RUN", "1")
	report, _, err := runPreflightReport(t, source, "--container-check", "--http-timeout", "50ms")
	if err == nil || report.Container != "failed" {
		t.Fatalf("startup failure was not reported: report=%#v err=%v", report, err)
	}
	requireFinding(t, &report, "container", "blocker")
	dockerLog := readDockerLog(t, log)
	for _, call := range []string{"build --platform linux/amd64", "run --platform linux/amd64", "rm -f ", "image rm -f "} {
		if !strings.Contains(dockerLog, call) {
			t.Fatalf("startup failure cleanup call %q missing from log:\n%s", call, dockerLog)
		}
	}
}

func TestContainerPreflightRejectsSourceChangedDuringCheck(t *testing.T) {
	source := completeSource(t, "")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, `{"status":"ok","commit_sha":%q}`, source.sha)
	}))
	defer server.Close()
	_, port, err := net.SplitHostPort(strings.TrimPrefix(server.URL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	_, log := writeFakeDocker(t, port)
	t.Setenv("FAKE_DOCKER_MUTATE_DIR", source.dir)
	report, _, err := runPreflightReport(t, source, "--container-check", "--http-timeout", "2s")
	if err == nil || report.Container != "failed" {
		t.Fatalf("source mutation was not rejected: report=%#v err=%v", report, err)
	}
	requireFinding(t, &report, "container", "blocker")
	dockerLog := readDockerLog(t, log)
	for _, call := range []string{"build --platform linux/amd64", "run --platform linux/amd64", "port ", "rm -f ", "image rm -f "} {
		if !strings.Contains(dockerLog, call) {
			t.Fatalf("source mutation cleanup call %q missing from log:\n%s", call, dockerLog)
		}
	}
}

func TestContainerPreflightCleanupFailureIsReported(t *testing.T) {
	source := completeSource(t, "")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, `{"status":"ok","commit_sha":%q}`, source.sha)
	}))
	defer server.Close()
	_, port, err := net.SplitHostPort(strings.TrimPrefix(server.URL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	_, log := writeFakeDocker(t, port)
	t.Setenv("FAKE_DOCKER_FAIL_CLEANUP", "1")
	report, _, err := runPreflightReport(t, source, "--container-check", "--http-timeout", "2s")
	if err == nil || report.Container != "failed" {
		t.Fatalf("cleanup failure was not reported: report=%#v err=%v", report, err)
	}
	finding := requireFinding(t, &report, "container", "blocker")
	if !strings.Contains(finding.Fix, "docker rm -f") {
		t.Fatalf("cleanup recovery command missing: %q", finding.Fix)
	}
	dockerLog := readDockerLog(t, log)
	for _, call := range []string{"build --platform linux/amd64", "run --platform linux/amd64", "port ", "rm -f ", "image rm -f "} {
		if !strings.Contains(dockerLog, call) {
			t.Fatalf("cleanup failure call %q missing from log:\n%s", call, dockerLog)
		}
	}
}

func TestContainerCheckSkipsDockerAfterStaticBlocker(t *testing.T) {
	source := makePreflightSource(t, "", map[string]string{"main.go": "package main\n"})
	_, log := writeFakeDocker(t, "1")
	report, _, err := runPreflightReport(t, source, "--container-check", "--http-timeout", "50ms")
	if err == nil || report.Container != "not_run_static_failed" || report.Static != "failed" {
		t.Fatalf("static blocker did not skip container check: report=%#v err=%v", report, err)
	}
	if dockerLog := readDockerLog(t, log); dockerLog != "" {
		t.Fatalf("Docker was invoked after a static blocker: %s", dockerLog)
	}
}

func TestBuildPreflightBlocksBeforeDocker(t *testing.T) {
	source := makePreflightSource(t, "", map[string]string{"main.go": "package main\n"})
	_, log := writeFakeDocker(t, "1")
	x := runner{
		o: options{
			repoID: "12", pr: "3", source: "owner/app", sha: source.sha,
			image: "ghcr.io/example-owner/previewmesh-c34-r12", sourceDir: source.dir,
			port: 8080, timeout: time.Second,
		},
		r: result{Namespace: "pm-r12-pr3"},
	}
	if err := x.build(); err == nil {
		t.Fatal("build unexpectedly passed without a Dockerfile")
	}
	if x.r.Preflight == nil {
		t.Fatal("build did not retain its preflight report")
	}
	requireFinding(t, x.r.Preflight, "dockerfile", "blocker")
	if dockerLog := readDockerLog(t, log); dockerLog != "" {
		t.Fatalf("Docker was invoked after a preflight blocker: %s", dockerLog)
	}
}
