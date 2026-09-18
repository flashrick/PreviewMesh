// Command control validates repository registrations and inspects pull requests.
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// registration is the trusted configuration for one source repository.
type registration struct {
	ID     string `json:"repository_id"`
	Source string `json:"source_repository"`
	Port   int    `json:"port"`
	Secret string `json:"source_secret"`
}

// state combines trusted registration data with the current pull request state.
type state struct {
	registration
	PR       string `json:"pr_number"`
	State    string `json:"state"`
	SHA      string `json:"sha"`
	Eligible bool   `json:"eligible"`
}

// api contains the GitHub API endpoint and authentication settings.
type api struct {
	base, token string
	client      *http.Client
}

// request sends one JSON GitHub API request and decodes an optional response.
func (a api) request(method, path string, body any, out any) error {
	var input io.Reader
	if body != nil {
		// Encode request bodies once so the API receives consistent JSON.
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		input = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, a.base+path, input)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+a.token)
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := a.client.Do(req)
	if err != nil {
		return errors.New("GitHub API request failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("GitHub API returned HTTP %d", resp.StatusCode)
	}
	if out != nil {
		// Bound response size before decoding data from the remote API.
		return json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(out)
	}
	return nil
}

// canonical matches the decimal form used by repository and PR identities.
var canonical = regexp.MustCompile(`^[1-9][0-9]*$`)

// fullSHA matches the exact commit format accepted by the control plane.
var fullSHA = regexp.MustCompile(`^[0-9a-f]{40}$`)

// resolve loads one exact repository registration from the trusted registry.
func resolve(path, id, source, pr string) (registration, error) {
	// Reject aliases and numeric overflow before consulting the registry.
	for _, s := range []string{id, pr} {
		if !canonical.MatchString(s) {
			return registration{}, errors.New("noncanonical identity")
		}
		if _, e := strconv.ParseUint(s, 10, 64); e != nil {
			return registration{}, errors.New("identity out of range")
		}
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return registration{}, err
	}
	var entries []registration
	if err = json.Unmarshal(b, &entries); err != nil {
		return registration{}, err
	}
	var selected registration
	count := 0
	for _, entry := range entries {
		if entry.ID == id {
			selected = entry
			count++
		}
	}
	// A duplicate ID or a source mismatch is not an unambiguous registration.
	if count != 1 || selected.Source != source {
		return registration{}, errors.New("repository not registered with this exact identity")
	}
	if selected.Port < 1 || selected.Port > 65535 || !regexp.MustCompile(`^[A-Z][A-Z0-9_]*$`).MatchString(selected.Secret) {
		// Secret names are environment-variable names used by later jobs.
		return registration{}, errors.New("invalid trusted registration")
	}
	return selected, nil
}

// inspect verifies the repository, PR, fork status, and author permission.
func inspect(a api, r registration, pr string) (state, error) {
	s := state{registration: r, PR: pr}
	var repo struct {
		ID   uint64 `json:"id"`
		Name string `json:"full_name"`
	}
	if err := a.request("GET", "/repositories/"+r.ID, nil, &repo); err != nil {
		return s, err
	}
	if strconv.FormatUint(repo.ID, 10) != r.ID || repo.Name != r.Source {
		// The numeric ID and full name must still agree with trusted config.
		return s, errors.New("GitHub repository identity changed; update trusted registration")
	}
	type ref struct {
		SHA  string `json:"sha"`
		Repo *struct {
			ID uint64 `json:"id"`
		} `json:"repo"`
	}
	var p struct {
		Number uint64 `json:"number"`
		State  string `json:"state"`
		Head   ref    `json:"head"`
		Base   ref    `json:"base"`
		User   struct {
			Login string `json:"login"`
		} `json:"user"`
	}
	if err := a.request("GET", "/repos/"+r.Source+"/pulls/"+pr, nil, &p); err != nil {
		return s, err
	}
	if strconv.FormatUint(p.Number, 10) != pr || p.Base.Repo == nil || p.Base.Repo.ID != repo.ID || !fullSHA.MatchString(p.Head.SHA) {
		return s, errors.New("invalid PR identity or head revision")
	}
	s.State = p.State
	s.SHA = p.Head.SHA
	// Closed PR cleanup does not depend on the original author's current access.
	if p.State == "closed" {
		return s, nil
	}
	if p.State != "open" {
		return s, errors.New("unknown PR state")
	}
	if p.Head.Repo == nil || p.Head.Repo.ID != repo.ID {
		// A fork head is not eligible for a preview deployment.
		return s, errors.New("fork PRs are not eligible")
	}
	var permission struct {
		User struct {
			Permissions struct {
				Push bool `json:"push"`
			} `json:"permissions"`
		} `json:"user"`
	}
	if err := a.request("GET", "/repos/"+r.Source+"/collaborators/"+url.PathEscape(p.User.Login)+"/permission", nil, &permission); err != nil {
		return s, err
	}
	if !permission.User.Permissions.Push {
		// Require effective push access instead of trusting a role label.
		return s, errors.New("PR author lacks effective write permission")
	}
	s.Eligible = true
	return s, nil
}

// outputs publishes validated state to GitHub Actions when requested.
func outputs(s state) error {
	if p := os.Getenv("GITHUB_OUTPUT"); p != "" {
		f, err := os.OpenFile(p, os.O_APPEND|os.O_WRONLY, 0600)
		if err != nil {
			return err
		}
		defer f.Close()
		_, err = fmt.Fprintf(f, "repository_id=%s\nsource_repository=%s\nsource_secret=%s\nport=%d\npr_number=%s\nstate=%s\nsha=%s\neligible=%t\n", s.ID, s.Source, s.Secret, s.Port, s.PR, s.State, s.SHA, s.Eligible)
		return err
	}
	return nil
}

// reportStatus keeps commit checks and PR conversation results independently observable.
func reportStatus(a api, r registration, pr, sha, status, description, target, runURL string, comment bool, evidence ...reportEvidence) error {
	if !fullSHA.MatchString(sha) || (status != "pending" && status != "success" && status != "failure") || len(description) > 140 {
		return errors.New("invalid commit status input")
	}
	// URLs are also embedded in Markdown; reject delimiters and credentials.
	validURL := func(raw string) bool {
		u, err := url.Parse(raw)
		return err == nil && (u.Scheme == "http" || u.Scheme == "https") && u.Hostname() != "" && u.User == nil && !strings.ContainsAny(raw, "<>\\\r\n\t ")
	}
	if !validURL(target) || (comment && (!validURL(runURL) || status == "pending")) {
		return errors.New("invalid reporting URL or nonterminal comment")
	}
	statusErr := a.request("POST", "/repos/"+r.Source+"/statuses/"+sha, map[string]string{"state": status, "context": "PreviewMesh", "description": description, "target_url": target}, nil)
	if statusErr != nil {
		statusErr = fmt.Errorf("commit status: %w", statusErr)
	}
	if !comment {
		return statusErr
	}
	// Append an attempt result so older revisions cannot overwrite newer feedback.
	escape := strings.NewReplacer("\\", "\\\\", "*", "\\*", "_", "\\_", "[", "\\[", "]", "\\]", "<", "&lt;", ">", "&gt;", "`", "\\`", "\n", " ", "\r", " ")
	body := fmt.Sprintf("### PreviewMesh deployment\n\nStatus: **%s** — %s\n\nCommit: `%s`\n\n[Workflow run](<%s>)\n", status, escape.Replace(description), sha, runURL)
	details := reportEvidence{}
	if len(evidence) > 0 {
		details = evidence[0]
	}
	body += details.markdown()
	if status == "success" && target != runURL {
		body += fmt.Sprintf("\n[Open preview](<%s>)\n\nThis preview URL requires access to the preview network and its hostname mapping.\n", target)
	}
	commentErr := a.request("POST", "/repos/"+r.Source+"/issues/"+pr+"/comments", map[string]string{"body": body}, nil)
	if commentErr != nil {
		commentErr = fmt.Errorf("PR comment: %w", commentErr)
	}
	return errors.Join(statusErr, commentErr)
}

// main resolves the registration and runs the selected control command.
func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: control resolve|inspect|status")
		os.Exit(2)
	}
	command := os.Args[1]
	f := flag.NewFlagSet(command, flag.ExitOnError)
	registry := f.String("registry", "config/repositories.json", "trusted registration")
	id := f.String("repository-id", "", "repository ID")
	source := f.String("source-repository", "", "registered owner/name")
	pr := f.String("pr", "", "PR number")
	sha := f.String("sha", "", "attempted SHA")
	status := f.String("state", "", "commit status")
	description := f.String("description", "", "status description")
	target := f.String("url", "", "status target")
	comment := f.Bool("comment", false, "also post a terminal result to the PR conversation")
	runURL := f.String("run-url", "", "workflow run URL for the PR comment")
	resultFile := f.String("result-file", "", "deployment or cleanup result JSON for PR feedback")
	buildState := f.String("build-state", "", "observed image build result")
	f.Parse(os.Args[2:])
	// Resolve identity before making any API request or status update.
	r, err := resolve(*registry, *id, *source, *pr)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	s := state{registration: r, PR: *pr}
	a := api{base: "https://api.github.com", token: os.Getenv("SOURCE_TOKEN"), client: &http.Client{Timeout: 20 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}
	if command != "resolve" && a.token == "" {
		fmt.Fprintln(os.Stderr, "SOURCE_TOKEN is missing")
		os.Exit(1)
	}
	switch command {
	case "resolve":
	case "inspect":
		s, err = inspect(a, r, *pr)
	case "status":
		details := reportEvidence{Build: *buildState}
		if *resultFile != "" {
			var data []byte
			data, err = os.ReadFile(*resultFile)
			if err == nil {
				err = json.Unmarshal(data, &details)
			}
			if err != nil {
				break
			}
		}
		err = reportStatus(a, r, *pr, *sha, *status, *description, *target, *runURL, *comment, details)
	default:
		err = errors.New("unknown control command")
	}
	if command == "resolve" || command == "inspect" {
		// Only trusted registration and validated API state are exposed to later jobs.
		if s.State != "open" && s.State != "closed" {
			s.State = ""
		}
		if !fullSHA.MatchString(s.SHA) {
			s.SHA = ""
		}
		if e := outputs(s); e != nil {
			err = errors.Join(err, e)
		}
		json.NewEncoder(os.Stdout).Encode(s)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, strings.TrimSpace(err.Error()))
		os.Exit(1)
	}
}
