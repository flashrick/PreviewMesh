// Command previewmesh manages isolated preview environments for pull requests.
package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/csv"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// domain prefixes labels and annotations owned by PreviewMesh.
const domain = "previewmesh.local/"

// Match canonical positive decimal identifiers.
var decimal = regexp.MustCompile(`^[1-9][0-9]*$`)

// Match a full lowercase Git commit SHA.
var shaPattern = regexp.MustCompile(`^[0-9a-f]{40}$`)

// Match an immutable SHA-256 image digest.
var digestPattern = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

// Match registered owner/name repository values.
var sourcePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+$`)

// Restrict images to the repository-specific GHCR namespace.
var imagePattern = regexp.MustCompile(`^ghcr\.io/[a-z0-9][a-z0-9._-]*/previewmesh-r([1-9][0-9]*)$`)

// options contains the validated command-line configuration.
type options struct {
	command, repoID, pr, source, sha, image, hostname, chart, sourceDir, evidence, resultFile, pullSecret string
	port                                                                                                  int
	timeout, httpTimeout                                                                                  time.Duration
}

// result is the structured outcome shared by the CLI and automation.
type result struct {
	Namespace    string `json:"namespace"`
	RequestedSHA string `json:"requested_sha"`
	ServedSHA    string `json:"served_sha"`
	Image        string `json:"image"`
	URL          string `json:"url"`
	Result       string `json:"result"`
	FailedStage  string `json:"failed_stage"`
	Rollback     string `json:"rollback"`
	Cleanup      string `json:"cleanup"`
	Error        string `json:"error"`
}

// namespace contains the Kubernetes fields needed for ownership checks.
type namespace struct {
	Metadata struct {
		UID               string            `json:"uid"`
		Labels            map[string]string `json:"labels"`
		Annotations       map[string]string `json:"annotations"`
		DeletionTimestamp *string           `json:"deletionTimestamp"`
	} `json:"metadata"`
}

// runner carries command options and accumulates the operation result.
type runner struct {
	o options
	r result
}

// identity builds the stable namespace and Helm release name for a preview.
func identity(repoID, pr string) (string, error) {
	// Validate before composing the name so aliases cannot create another identity.
	for _, s := range []string{repoID, pr} {
		if !decimal.MatchString(s) {
			return "", errors.New("repository ID and PR must be canonical positive integers")
		}
		if _, err := strconv.ParseUint(s, 10, 64); err != nil {
			return "", errors.New("identifier exceeds uint64")
		}
	}
	name := "pm-r" + repoID + "-pr" + pr
	// Leave room for Helm's generated name suffixes and metadata.
	if len(name) > 53 {
		return "", errors.New("identity exceeds Helm release name limit")
	}
	return name, nil
}

// parse validates command-line input before any external side effect occurs.
func parse(args []string) (options, error) {
	var o options
	if len(args) == 0 {
		return o, errors.New("usage: previewmesh build|deploy|verify|cleanup [flags]")
	}
	o.command = args[0]
	// Use the command name for clearer flag errors and help output.
	if o.command != "build" && o.command != "deploy" && o.command != "verify" && o.command != "cleanup" {
		return o, errors.New("unknown command")
	}
	f := flag.NewFlagSet(o.command, flag.ContinueOnError)
	f.StringVar(&o.repoID, "repository-id", "", "immutable GitHub repository ID")
	f.StringVar(&o.pr, "pr", "", "PR number")
	f.StringVar(&o.source, "source-repository", "", "registered owner/name")
	f.StringVar(&o.sha, "sha", "", "full source commit SHA")
	f.StringVar(&o.image, "image", "", "GHCR repository for build; digest reference for deploy")
	f.StringVar(&o.hostname, "hostname", "", "expected preview hostname")
	f.StringVar(&o.chart, "chart", "charts/preview", "trusted control chart")
	f.StringVar(&o.sourceDir, "source-dir", ".", "clean checked-out source directory")
	f.StringVar(&o.evidence, "evidence", "", "per-job CSV path")
	f.StringVar(&o.resultFile, "result-file", "", "JSON result path")
	f.StringVar(&o.pullSecret, "pull-secret", "ghcr-pull", "existing image pull secret; empty for public image")
	f.IntVar(&o.port, "port", 8080, "registered application HTTP port")
	f.DurationVar(&o.timeout, "timeout", 5*time.Minute, "timeout per external operation")
	f.DurationVar(&o.httpTimeout, "http-timeout", time.Minute, "HTTP verification deadline")
	if err := f.Parse(args[1:]); err != nil {
		return o, err
	}
	if f.NArg() != 0 {
		return o, errors.New("unexpected positional arguments")
	}
	name, err := identity(o.repoID, o.pr)
	if err != nil {
		return o, err
	}
	if !sourcePattern.MatchString(o.source) {
		return o, errors.New("source-repository must be registered owner/name")
	}
	if o.timeout <= 0 || o.httpTimeout <= 0 {
		return o, errors.New("timeouts must be positive")
	}
	if o.pullSecret != "" && (len(o.pullSecret) > 253 || !regexp.MustCompile(`^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$`).MatchString(o.pullSecret)) {
		return o, errors.New("pull-secret must be a Kubernetes Secret name")
	}
	if o.port < 1 || o.port > 65535 {
		return o, errors.New("port must be between 1 and 65535")
	}
	if o.hostname == "" {
		// Derive the hostname from the same identity used for the namespace.
		o.hostname = name + ".preview.test"
	}
	if o.hostname != name+".preview.test" {
		return o, errors.New("hostname must match preview identity")
	}
	if o.command != "cleanup" && !shaPattern.MatchString(o.sha) {
		return o, errors.New("sha must be 40 lowercase hexadecimal characters")
	}
	if o.command == "build" || o.command == "deploy" {
		// Build accepts a repository, while deploy requires its immutable digest.
		parts := strings.Split(o.image, "@")
		match := imagePattern.FindStringSubmatch(parts[0])
		if len(match) != 2 || match[1] != o.repoID {
			return o, errors.New("image must use ghcr.io/OWNER/previewmesh-r<repository-id>")
		}
		if o.command == "build" && len(parts) != 1 {
			return o, errors.New("build image must not contain a tag or digest")
		}
		if o.command == "deploy" && (len(parts) != 2 || !digestPattern.MatchString(parts[1])) {
			return o, errors.New("deploy requires an immutable sha256 image digest")
		}
	}
	return o, nil
}

// run executes a trusted external tool with the operation timeout.
func (x *runner) run(input []byte, command string, args ...string) ([]byte, error) {
	// Keep one deadline for kubectl, Helm, Git, and Docker calls.
	ctx, cancel := context.WithTimeout(context.Background(), x.o.timeout)
	defer cancel()
	c := exec.CommandContext(ctx, command, args...)
	c.WaitDelay = time.Second
	if input != nil {
		c.Stdin = bytes.NewReader(input)
	}
	var output bytes.Buffer
	c.Stdout = &output
	if command == "docker" {
		// The build result is read from the metadata file, not Docker's stdout.
		c.Stdout = io.Discard
	}
	c.Stderr = io.Discard
	if err := c.Run(); err != nil {
		// External logs may contain credentials or application output; retain them only at their source.
		if ctx.Err() != nil {
			return nil, fmt.Errorf("%s timed out: %w", command, ctx.Err())
		}
		return nil, fmt.Errorf("%s failed: %w", command, err)
	}
	return output.Bytes(), nil
}

// stage runs one operation stage and records optional audit evidence.
func (x *runner) stage(name string, fn func() error) error {
	start := time.Now()
	err := fn()
	end := time.Now()
	outcome := "success"
	message := ""
	if err != nil {
		outcome = "failure"
		message = err.Error()
		// Preserve the first failed stage even if recovery also fails.
		if x.r.FailedStage == "" {
			x.r.FailedStage = name
		}
	}
	if x.o.evidence != "" {
		// Evidence is append-only so each stage keeps its timing and outcome.
		if e := os.MkdirAll(filepath.Dir(x.o.evidence), 0700); e != nil {
			return errors.Join(err, e)
		}
		f, e := os.OpenFile(x.o.evidence, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0600)
		if e != nil {
			return errors.Join(err, e)
		}
		stat, e := f.Stat()
		if e != nil {
			f.Close()
			return errors.Join(err, e)
		}
		w := csv.NewWriter(f)
		if stat.Size() == 0 {
			e = w.Write([]string{"run_id", "attempt", "repository_id", "source_repository", "pr_number", "requested_sha", "served_sha", "stage", "started_at_utc", "ended_at_utc", "duration_seconds", "result", "error"})
		}
		if e == nil {
			e = w.Write([]string{os.Getenv("GITHUB_RUN_ID"), os.Getenv("GITHUB_RUN_ATTEMPT"), x.o.repoID, x.o.source, x.o.pr, x.o.sha, x.r.ServedSHA, name, start.UTC().Format(time.RFC3339Nano), end.UTC().Format(time.RFC3339Nano), strconv.FormatFloat(end.Sub(start).Seconds(), 'f', 6, 64), outcome, message})
		}
		w.Flush()
		e = errors.Join(e, w.Error(), f.Close())
		if e != nil {
			return errors.Join(err, fmt.Errorf("write evidence: %w", e))
		}
	}
	return err
}

// getNS reads the preview namespace and verifies its ownership labels.
func (x *runner) getNS() (*namespace, error) {
	b, err := x.run(nil, "kubectl", "get", "namespace", x.r.Namespace, "--ignore-not-found", "-o", "json")
	if err != nil {
		return nil, err
	}
	if len(bytes.TrimSpace(b)) == 0 {
		// --ignore-not-found represents an absent namespace as empty output.
		return nil, nil
	}
	var ns namespace
	if err = json.Unmarshal(b, &ns); err != nil {
		return nil, err
	}
	if ns.Metadata.Labels[domain+"managed-by"] != "previewmesh" || ns.Metadata.Labels[domain+"repository-id"] != x.o.repoID || ns.Metadata.Labels[domain+"pr-number"] != x.o.pr {
		// Never operate on a namespace that belongs to another identity.
		return nil, errors.New("namespace ownership mismatch")
	}
	return &ns, nil
}

// annotate updates only PreviewMesh-owned namespace annotations.
func (x *runner) annotate(values map[string]string) error {
	// Merge only our annotations, preserving namespace state owned by Kubernetes.
	b, _ := json.Marshal(map[string]any{"metadata": map[string]any{"annotations": values}})
	_, err := x.run(nil, "kubectl", "patch", "namespace", x.r.Namespace, "--type=merge", "-p", string(b))
	return err
}

// ensureNS returns an owned namespace or creates a new one.
func (x *runner) ensureNS() (*namespace, error) {
	ns, err := x.getNS()
	if err != nil {
		return nil, err
	}
	if ns != nil {
		// A terminating namespace cannot safely receive a new release.
		if ns.Metadata.DeletionTimestamp != nil {
			return nil, errors.New("namespace is terminating")
		}
		return ns, nil
	}
	sourceName := strings.Split(x.o.source, "/")[1]
	// Keep the source label within Kubernetes' length limit.
	if len(sourceName) > 63 {
		sourceName = sourceName[:63]
	}
	sourceName = strings.Trim(sourceName, "-_.")
	b, _ := json.Marshal(map[string]any{"apiVersion": "v1", "kind": "Namespace", "metadata": map[string]any{"name": x.r.Namespace, "labels": map[string]string{domain + "managed-by": "previewmesh", domain + "repository-id": x.o.repoID, domain + "pr-number": x.o.pr, domain + "source-name": sourceName}, "annotations": map[string]string{domain + "source-repository": x.o.source}}})
	// Create never adopts or relabels a pre-existing namespace.
	if _, err = x.run(b, "kubectl", "create", "-f", "-"); err != nil {
		return nil, err
	}
	return x.getNS()
}

// build verifies the checkout, pushes an image, and records its digest.
func (x *runner) build() error {
	return x.stage("build_push", func() error {
		abs, err := filepath.Abs(x.o.sourceDir)
		if err != nil {
			return err
		}
		b, err := x.run(nil, "git", "-C", abs, "rev-parse", "HEAD")
		if err != nil {
			return err
		}
		// The checkout must be both at the requested commit and clean.
		if strings.TrimSpace(string(b)) != x.o.sha {
			return errors.New("checked-out HEAD does not match requested SHA")
		}
		b, err = x.run(nil, "git", "-C", abs, "status", "--porcelain", "--untracked-files=all", "--ignored")
		if err != nil {
			return err
		}
		if len(bytes.TrimSpace(b)) != 0 {
			return errors.New("source checkout must be clean")
		}
		meta, err := os.CreateTemp("", "previewmesh-build-*.json")
		if err != nil {
			return err
		}
		path := meta.Name()
		meta.Close()
		defer os.Remove(path)
		tag := x.o.image + ":pr-" + x.o.pr + "-" + x.o.sha
		_, err = x.run(nil, "docker", "buildx", "build", "--platform", "linux/amd64", "--push", "--metadata-file", path, "--tag", tag, "--file", filepath.Join(abs, "Dockerfile"), abs)
		if err != nil {
			return err
		}
		b, err = os.ReadFile(path)
		if err != nil {
			return err
		}
		var metadata map[string]json.RawMessage
		if err = json.Unmarshal(b, &metadata); err != nil {
			return err
		}
		var digest string
		if err = json.Unmarshal(metadata["containerimage.digest"], &digest); err != nil {
			return err
		}
		if !digestPattern.MatchString(digest) {
			return errors.New("build returned invalid image digest")
		}
		// Deploy only the immutable digest returned by buildx.
		x.r.Image = x.o.image + "@" + digest
		return nil
	})
}

// health polls the preview until it serves the requested commit.
func health(ctx context.Context, url, sha string) (string, error) {
	client := &http.Client{Timeout: 5 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	last := "no response"
	served := ""
	for {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, url+"/health", nil)
		if err != nil {
			return served, err
		}
		resp, err := client.Do(req)
		if err != nil {
			// Ending the verification window is not a new endpoint failure.
			if ctx.Err() == nil || last == "no response" {
				last = "HTTP request failed"
			}
		} else {
			var body struct {
				Status string `json:"status"`
				SHA    string `json:"commit_sha"`
			}
			data, readErr := io.ReadAll(io.LimitReader(resp.Body, 65537))
			resp.Body.Close()
			if readErr != nil && ctx.Err() != nil {
				if last == "no response" {
					last = "HTTP request failed"
				}
				return served, fmt.Errorf("HTTP verification failed: %s", last)
			}
			err = json.Unmarshal(data, &body)
			if readErr != nil || len(data) > 65536 {
				err = errors.New("invalid health response body")
			}
			// Keep a valid served SHA for failure diagnostics.
			if err == nil && shaPattern.MatchString(body.SHA) {
				served = body.SHA
			}
			if resp.StatusCode == 200 && err == nil && body.Status == "ok" && body.SHA == sha {
				return body.SHA, nil
			}
			last = "health response did not match expected status and revision"
		}
		select {
		case <-ctx.Done():
			return served, fmt.Errorf("HTTP verification failed: %s", last)
		case <-time.After(time.Second):
		}
	}
}

// check waits for rollout readiness and then verifies the application response.
func (x *runner) check(sha string) error {
	if _, err := x.run(nil, "kubectl", "rollout", "status", "deployment/"+x.r.Namespace, "-n", x.r.Namespace, "--timeout="+x.o.timeout.String()); err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), x.o.httpTimeout)
	defer cancel()
	served, err := health(ctx, x.r.URL, sha)
	x.r.ServedSHA = served
	return err
}

// revision returns the current Helm release revision.
func (x *runner) revision() (string, error) {
	b, err := x.run(nil, "helm", "status", x.r.Namespace, "-n", x.r.Namespace, "-o", "json")
	if err != nil {
		return "", err
	}
	var state struct {
		Version int `json:"version"`
	}
	if err = json.Unmarshal(b, &state); err != nil {
		return "", err
	}
	if state.Version < 1 {
		return "", errors.New("invalid Helm revision")
	}
	return strconv.Itoa(state.Version), nil
}

// markGood records the revision and commit after successful verification.
func (x *runner) markGood(sha string) error {
	rev, err := x.revision()
	if err != nil {
		return err
	}
	return x.annotate(map[string]string{domain + "verified-revision": rev, domain + "verified-sha": sha, domain + "state": "ready"})
}

// recover marks a failed release and restores the last verified revision.
func (x *runner) recover(ns *namespace, cause error) error {
	// Mark failure before rollback so observers see the attempted recovery.
	markErr := x.annotate(map[string]string{domain + "state": "failed"})
	rev := ns.Metadata.Annotations[domain+"verified-revision"]
	sha := ns.Metadata.Annotations[domain+"verified-sha"]
	// Rollback is possible only when both last-good annotations are valid.
	if !decimal.MatchString(rev) || !shaPattern.MatchString(sha) {
		x.r.Rollback = "not_available"
		return errors.Join(cause, markErr)
	}
	err := x.stage("rollback", func() error {
		if _, e := x.run(nil, "helm", "rollback", x.r.Namespace, rev, "-n", x.r.Namespace, "--wait", "--timeout", x.o.timeout.String()); e != nil {
			return e
		}
		if e := x.check(sha); e != nil {
			return e
		}
		return x.markGood(sha)
	})
	if err != nil {
		x.r.Rollback = "failed"
		return errors.Join(cause, markErr, fmt.Errorf("rollback failed: %w", err))
	}
	x.r.Rollback = "verified"
	return errors.Join(cause, markErr)
}

// pullSecret verifies or creates the image pull secret used by Helm.
func (x *runner) pullSecret() error {
	if x.o.pullSecret == "" {
		return nil
	}
	user, token := os.Getenv("PREVIEWMESH_GHCR_USER"), os.Getenv("PREVIEWMESH_GHCR_TOKEN")
	if user == "" && token == "" {
		// Without credentials, require the named secret to already exist.
		_, err := x.run(nil, "kubectl", "get", "secret", x.o.pullSecret, "-n", x.r.Namespace)
		if err != nil {
			return errors.New("private image pull secret missing; configure PREVIEWMESH_GHCR_USER and PREVIEWMESH_GHCR_TOKEN")
		}
		return nil
	}
	if user == "" || token == "" {
		return errors.New("both GHCR user and read token are required")
	}
	// Send credentials through stdin so they never appear in command arguments.
	auth := base64.StdEncoding.EncodeToString([]byte(user + ":" + token))
	config, _ := json.Marshal(map[string]any{"auths": map[string]any{"ghcr.io": map[string]string{"auth": auth}}})
	secret, _ := json.Marshal(map[string]any{"apiVersion": "v1", "kind": "Secret", "metadata": map[string]string{"name": x.o.pullSecret, "namespace": x.r.Namespace}, "type": "kubernetes.io/dockerconfigjson", "data": map[string]string{".dockerconfigjson": base64.StdEncoding.EncodeToString(config)}})
	// Secret bytes travel on stdin, never command arguments or evidence files.
	_, err := x.run(secret, "kubectl", "apply", "--server-side", "--field-manager=previewmesh", "-f", "-")
	return err
}

// deploy creates or updates the preview and verifies the served commit.
func (x *runner) deploy() error {
	ns, err := x.ensureNS()
	if err != nil {
		return err
	}
	if err = x.annotate(map[string]string{domain + "state": "deploying"}); err != nil {
		return err
	}
	err = x.stage("deploy", func() error {
		if e := x.pullSecret(); e != nil {
			return e
		}
		values := map[string]any{"image": x.o.image, "commitSHA": x.o.sha, "port": x.o.port, "hostname": x.o.hostname, "imagePullSecret": x.o.pullSecret}
		// Keep Helm values out of process arguments and evidence logs.
		f, e := os.CreateTemp("", "previewmesh-values-*.json")
		if e != nil {
			return e
		}
		defer os.Remove(f.Name())
		if e = json.NewEncoder(f).Encode(values); e != nil {
			f.Close()
			return e
		}
		if e = f.Close(); e != nil {
			return e
		}
		// Keep Helm history so rollback can target the last verified revision.
		_, e = x.run(nil, "helm", "upgrade", "--install", x.r.Namespace, x.o.chart, "-n", x.r.Namespace, "--values", f.Name(), "--wait", "--timeout", x.o.timeout.String(), "--history-max", "0")
		return e
	})
	if err != nil {
		return x.recover(ns, err)
	}
	if err = x.stage("http_verify", func() error { return x.check(x.o.sha) }); err != nil {
		return x.recover(ns, err)
	}
	return x.markGood(x.o.sha)
}

// verify checks an existing preview without changing its desired revision.
func (x *runner) verify() error {
	ns, err := x.getNS()
	if err != nil {
		return err
	}
	if ns == nil {
		return errors.New("namespace does not exist")
	}
	if err = x.stage("http_verify", func() error { return x.check(x.o.sha) }); err != nil {
		return x.recover(ns, err)
	}
	return x.markGood(x.o.sha)
}

// cleanup deletes only an owned namespace and confirms that it is gone.
func (x *runner) cleanup() error {
	return x.stage("cleanup", func() error {
		ns, err := x.getNS()
		if err != nil {
			return err
		}
		if ns == nil {
			x.r.Cleanup = "confirmed_absent"
			return nil
		}
		if ns.Metadata.UID == "" {
			return errors.New("namespace UID is missing")
		}
		// Bind deletion to this UID so a recreated namespace cannot be removed.
		options, _ := json.Marshal(map[string]any{"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": map[string]string{"uid": ns.Metadata.UID}})
		if _, err = x.run(options, "kubectl", "delete", "--raw", "/api/v1/namespaces/"+x.r.Namespace, "-f", "-"); err != nil {
			return err
		}
		if _, err = x.run(nil, "kubectl", "wait", "--for=delete", "namespace/"+x.r.Namespace, "--timeout="+x.o.timeout.String()); err != nil {
			return err
		}
		// Confirm absence; API and permission errors are never treated as deletion success.
		ns, err = x.getNS()
		if err != nil {
			return err
		}
		if ns != nil {
			return errors.New("namespace remains after deletion")
		}
		x.r.Cleanup = "confirmed_absent"
		return nil
	})
}

// execute dispatches one validated command and normalizes its result.
func execute(o options) (result, error) {
	name, _ := identity(o.repoID, o.pr)
	// Initialize fields that should be explicit even when a command skips them.
	x := runner{o: o, r: result{Namespace: name, RequestedSHA: o.sha, Image: o.image, URL: "http://" + o.hostname, Rollback: "not_attempted", Cleanup: "not_attempted"}}
	var err error
	switch o.command {
	case "build":
		err = x.build()
	case "deploy":
		err = x.deploy()
	case "verify":
		err = x.verify()
	case "cleanup":
		err = x.cleanup()
	}
	// Return a structured failure while preserving the stage-specific details.
	x.r.Result = "success"
	if err != nil {
		x.r.Result = "failure"
		x.r.Error = err.Error()
		if x.r.FailedStage == "" {
			x.r.FailedStage = o.command
		}
	}
	return x.r, err
}

// main parses input, runs the command, and prints the structured result.
func main() {
	o, err := parse(os.Args[1:])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	r, err := execute(o)
	// Always print JSON so automation can inspect failures consistently.
	b, _ := json.MarshalIndent(r, "", "  ")
	if o.resultFile != "" {
		e := os.MkdirAll(filepath.Dir(o.resultFile), 0700)
		if e == nil {
			e = os.WriteFile(o.resultFile, append(b, '\n'), 0600)
		}
		if e != nil {
			err = errors.Join(err, e)
			fmt.Fprintln(os.Stderr, "cannot write result file")
		}
	}
	fmt.Println(string(b))
	if err != nil {
		os.Exit(1)
	}
}
