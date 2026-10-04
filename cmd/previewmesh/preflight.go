package main

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// Reports contain only controlled diagnostics, never source snippets or tool logs.
type preflightFinding struct {
	Check    string `json:"check"`
	Severity string `json:"severity"`
	Fix      string `json:"fix"`
}

type preflightReport struct {
	SHA                    string             `json:"source_sha"`
	Fingerprint            string             `json:"configuration_fingerprint"`
	CheckedAt              string             `json:"checked_at"`
	Port                   int                `json:"port"`
	Contract               string             `json:"contract"`
	Static                 string             `json:"static"`
	Container              string             `json:"container"`
	ContainerConfiguration string             `json:"container_configuration,omitempty"`
	Deployment             string             `json:"deployment"`
	Findings               []preflightFinding `json:"findings"`
}

// Suppress arbitrary build/application output, including credentials embedded in errors.
func preflightTool(timeout time.Duration, command string, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	c := exec.CommandContext(ctx, command, args...)
	c.WaitDelay = time.Second
	if command == "docker" && (len(args) == 0 || args[0] != "port") {
		// Build logs can be arbitrarily large and contain application secrets.
		c.Stdout, c.Stderr = io.Discard, io.Discard
		if err := c.Run(); err != nil {
			return nil, errors.New("docker unavailable, failed or timed out; check installation and configuration")
		}
		return nil, nil
	}
	b, err := c.Output()
	if err != nil {
		return nil, fmt.Errorf("%s unavailable, failed or timed out; check installation and configuration", command)
	}
	return b, nil
}

func (r *preflightReport) finding(check, severity, fix string) {
	r.Findings = append(r.Findings, preflightFinding{check, severity, fix})
	if severity == "blocker" {
		r.Static = "failed"
	} else if r.Static != "failed" {
		r.Static = "passed_with_warnings"
	}
}

// A clean repository binds all inspected files to the reported commit, including ignored files.
func inspectApplication(dir, expected string, port int, timeout time.Duration) (*preflightReport, error) {
	r := &preflightReport{Port: port, Contract: "v1: 0.0.0.0; GET /health HTTP 200 {status:ok,commit_sha:PREVIEW_COMMIT_SHA}; UID/GID 65532; cap-drop ALL; no-new-privileges", CheckedAt: time.Now().UTC().Format(time.RFC3339), Static: "passed", Container: "not_requested", Deployment: "not_attempted", Findings: []preflightFinding{}}
	fail := func(check, fix string) (*preflightReport, error) {
		r.finding(check, "blocker", fix)
		return r, errors.New("application preflight blocked; resolve the reported blockers before building or deploying")
	}
	if port < 1 || port > 65535 {
		return fail("port", "Set the registered application HTTP port to an integer from 1 to 65535.")
	}
	abs, err := filepath.EvalSymlinks(dir)
	if err != nil {
		return fail("source", "Provide an existing source checkout.")
	}
	abs, err = filepath.Abs(abs)
	if err != nil {
		return fail("source", "Provide an existing source checkout.")
	}
	root, err := preflightTool(timeout, "git", "-C", abs, "rev-parse", "--show-toplevel")
	if err != nil || strings.TrimSpace(string(root)) != abs {
		return fail("source", "Use the root of a Git source checkout and install Git.")
	}
	head, err := preflightTool(timeout, "git", "-C", abs, "rev-parse", "HEAD")
	r.SHA = strings.TrimSpace(string(head))
	if err != nil || !shaPattern.MatchString(r.SHA) {
		r.SHA = ""
		return fail("revision", "Commit the source files so HEAD resolves to a full Git SHA.")
	}
	if expected != "" && expected != r.SHA {
		return fail("revision", "Check out the requested SHA and rerun preflight.")
	}
	r.Fingerprint = fmt.Sprintf("%x", sha256.Sum256([]byte(fmt.Sprintf("%s\n%d\n%s", r.SHA, port, r.Contract))))
	status, err := preflightTool(timeout, "git", "-C", abs, "status", "--porcelain", "--untracked-files=all", "--ignored")
	if err != nil || len(status) != 0 {
		return fail("clean_checkout", "Use a clean checkout, including untracked and ignored files; commit intended changes and store reports outside the source checkout.")
	}
	info, err := os.Lstat(filepath.Join(abs, "Dockerfile"))
	if err != nil || !info.Mode().IsRegular() || info.Size() > 1024*1024 {
		return fail("dockerfile", "Add a regular root Dockerfile smaller than 1 MiB; symlink Dockerfiles are not accepted.")
	}
	dockerfile, err := os.ReadFile(filepath.Join(abs, "Dockerfile"))
	if err != nil {
		return fail("dockerfile", "Make the root Dockerfile readable.")
	}
	// Only the final stage's declarations describe the image that will run.
	var exposed []string
	user := ""
	hasFrom := false
	for _, line := range strings.Split(strings.ReplaceAll(string(dockerfile), "\\\n", " "), "\n") {
		fields := strings.Fields(line)
		if len(fields) < 2 {
			continue
		}
		switch strings.ToUpper(fields[0]) {
		case "FROM":
			hasFrom = true
			exposed = nil
			user = ""
		case "EXPOSE":
			exposed = append(exposed, fields[1:]...)
		case "USER":
			user = fields[1]
		}
	}
	if !hasFrom {
		return fail("dockerfile", "Add a valid FROM instruction; use the optional container check to validate the complete Docker build.")
	}
	matching := false
	for _, value := range exposed {
		if strings.TrimSuffix(value, "/tcp") == strconv.Itoa(port) {
			matching = true
		}
	}
	if !matching {
		r.finding("port", "warning", "The final Dockerfile stage does not explicitly EXPOSE the registered TCP port. Align EXPOSE and the app listener with the reported port; inherited or variable ports require runtime verification.")
	}
	if user != "65532" && user != "65532:65532" {
		r.finding("non_root", "warning", "The final USER does not explicitly match UID/GID 65532. Ensure files and startup commands work as 65532:65532 without capabilities; use USER 65532:65532 and writable application directories.")
	}
	if port < 1024 {
		r.finding("privileged_port", "warning", "Use an application port >=1024 or verify low-port binding works with UID 65532 and all capabilities dropped.")
	}
	files, err := preflightTool(timeout, "git", "-C", abs, "ls-files", "-z")
	if err != nil {
		return fail("source", "Make tracked source files available to Git and rerun.")
	}
	var source strings.Builder
	skipped := false
	// Bound scanning and skip symlinks, dependencies, docs and tests to avoid misleading evidence.
	for _, name := range strings.Split(string(files), "\x00") {
		ext := filepath.Ext(name)
		if !strings.Contains("|.go|.py|.js|.ts|.tsx|.jsx|.rs|.java|.cs|.rb|.sh|.json|.yaml|.yml|", "|"+ext+"|") || strings.Contains(name, "test") || strings.HasPrefix(name, "docs/") || strings.Contains(name, "vendor/") || strings.Contains(name, "node_modules/") {
			continue
		}
		path := filepath.Join(abs, name)
		resolved, e := filepath.EvalSymlinks(path)
		if e != nil || resolved != path {
			skipped = true
			continue
		}
		fi, e := os.Lstat(path)
		if e != nil || !fi.Mode().IsRegular() || fi.Size() > 1024*1024 || int64(source.Len())+fi.Size() > 8*1024*1024 {
			skipped = true
			continue
		}
		b, e := os.ReadFile(path)
		if e != nil {
			skipped = true
			continue
		}
		source.Write(b)
		source.WriteByte('\n')
	}
	content := string(dockerfile) + "\n" + source.String()
	if skipped {
		r.finding("scan_scope", "warning", "Some source files could not be scanned or exceeded size limits. Review them manually and run the optional container check.")
	}
	if strings.Contains(content, "127.0.0.1") || strings.Contains(content, "localhost") {
		r.finding("loopback", "warning", "Loopback references were found and may be client addresses. Verify the application listener binds 0.0.0.0, not localhost or 127.0.0.1.")
	}
	for _, check := range []struct{ id, marker, fix string }{
		{"listen_address", "0.0.0.0", "Confirm the application binds 0.0.0.0 on the registered HTTP port; no explicit wildcard address was found in scanned source."},
		{"health", "/health", "Implement GET /health with HTTP 200 and JSON status=ok and commit_sha; no /health marker was found in scanned source."},
		{"commit_sha", "PREVIEW_COMMIT_SHA", "Read PREVIEW_COMMIT_SHA at runtime and return it as commit_sha from /health; no environment variable marker was found."},
		{"health_payload", "commit_sha", "Return JSON commit_sha from /health, using PREVIEW_COMMIT_SHA rather than a baked-in revision."},
	} {
		if !strings.Contains(content, check.marker) {
			r.finding(check.id, "warning", check.fix)
		}
	}
	r.finding("static_limits", "warning", "Static conditions only: source markers cannot prove listener, health response, environment propagation or file permissions. Resolve warnings before deployment; opt into --container-check to test startup. No preview has been deployed.")
	return r, nil
}

// Runtime checks use a unique local image and container; they never push or publish a preview.
func checkApplicationContainer(r *preflightReport, dir string, timeout, httpTimeout time.Duration) (err error) {
	r.Container = "failed"
	r.ContainerConfiguration = fmt.Sprintf("linux/amd64; UID/GID=65532; cap-drop=ALL; no-new-privileges; memory=512m; cpus=1; pids=128; loopback-only publication; operation-timeout=%s; health-timeout=%s", timeout, httpTimeout)
	r.Fingerprint = fmt.Sprintf("%x", sha256.Sum256([]byte(r.Fingerprint+"\n"+r.ContainerConfiguration)))
	tmp, err := os.MkdirTemp("", "previewmesh-preflight-")
	if err != nil {
		return errors.New("cannot allocate temporary container check workspace")
	}
	defer os.RemoveAll(tmp)
	name := filepath.Base(tmp)
	image := name + ":check"
	buildAttempted, runAttempted := false, false
	defer func() {
		// Cleanup uses a fresh deadline even when build/start/health has timed out.
		var containerErr, imageErr error
		if runAttempted {
			_, containerErr = preflightTool(30*time.Second, "docker", "rm", "-f", name)
		}
		if buildAttempted {
			_, imageErr = preflightTool(30*time.Second, "docker", "image", "rm", "-f", image)
		}
		if containerErr != nil || imageErr != nil {
			r.Container = "failed"
			err = errors.Join(err, fmt.Errorf("temporary resource cleanup could not be confirmed; run docker rm -f %s and docker image rm -f %s", name, image))
		}
	}()
	// A tag is allocated before building so partial or timed-out builds can still be cleaned up.
	buildAttempted = true
	if _, err = preflightTool(timeout, "docker", "build", "--platform", "linux/amd64", "--tag", image, "--file", filepath.Join(dir, "Dockerfile"), dir); err != nil {
		return err
	}
	runAttempted = true
	if _, err = preflightTool(timeout, "docker", "run", "--platform", "linux/amd64", "--detach", "--name", name, "--user", "65532:65532", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "128", "--memory", "512m", "--cpus", "1", "--env", "PREVIEW_COMMIT_SHA="+r.SHA, "--publish", fmt.Sprintf("127.0.0.1::%d", r.Port), image); err != nil {
		return err
	}
	b, err := preflightTool(timeout, "docker", "port", name, strconv.Itoa(r.Port)+"/tcp")
	if err != nil {
		return err
	}
	address := strings.TrimSpace(string(b))
	if !strings.HasPrefix(address, "127.0.0.1:") {
		return errors.New("container check did not receive a loopback-only port")
	}
	port, err := strconv.Atoi(strings.TrimPrefix(address, "127.0.0.1:"))
	if err != nil || port < 1 || port > 65535 {
		return errors.New("container check returned an invalid port")
	}
	ctx, cancel := context.WithTimeout(context.Background(), httpTimeout)
	defer cancel()
	if _, err = health(ctx, "http://"+address, r.SHA); err != nil {
		return errors.New("container startup/health failed: check listener port, 0.0.0.0 binding, UID 65532 file access and /health status/commit_sha from PREVIEW_COMMIT_SHA")
	}
	// Detect edits made while Docker was building; old success must not describe new source.
	if _, err = inspectApplication(dir, r.SHA, r.Port, timeout); err != nil {
		return errors.New("source changed during container check; rerun from the intended clean commit")
	}
	r.Container = "passed"
	return nil
}

func runPreflight(args []string, out io.Writer) error {
	f := flag.NewFlagSet("preflight", flag.ContinueOnError)
	dir := f.String("source-dir", ".", "clean source checkout root")
	port := f.Int("port", 8080, "registered application HTTP port")
	sha := f.String("sha", "", "expected full source SHA (default HEAD)")
	container := f.Bool("container-check", false, "explicitly build and run application code locally; no deployment")
	timeout := f.Duration("timeout", 5*time.Minute, "timeout per external operation")
	httpTimeout := f.Duration("http-timeout", time.Minute, "container startup/health deadline")
	if err := f.Parse(args); err != nil {
		return err
	}
	if f.NArg() != 0 || *timeout <= 0 || *httpTimeout <= 0 || (*sha != "" && !shaPattern.MatchString(*sha)) {
		return errors.New("invalid arguments: use a full lowercase SHA and positive timeouts")
	}
	abs, err := filepath.Abs(*dir)
	if err != nil {
		return errors.New("invalid source directory")
	}
	r, err := inspectApplication(abs, *sha, *port, *timeout)
	if err != nil && *container {
		r.Container = "not_run_static_failed"
	}
	if err == nil && *container {
		err = checkApplicationContainer(r, abs, *timeout, *httpTimeout)
	}
	if err != nil && r.Container == "failed" {
		r.Findings = append(r.Findings, preflightFinding{"container", "blocker", err.Error()})
	}
	encoder := json.NewEncoder(out)
	encoder.SetIndent("", "  ")
	return errors.Join(err, encoder.Encode(r))
}
