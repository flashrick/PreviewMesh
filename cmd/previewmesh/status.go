package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/url"
	"os/exec"
	"strconv"
	"strings"
	"time"

	"previewmesh/internal/statusrecord"
)

// githubRead keeps authentication inside gh; neither tokens nor raw errors enter output.
func githubRead(endpoint string, pages bool) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	args := []string{"api", "--hostname", "github.com", endpoint}
	if pages {
		args = append(args, "--paginate", "--slurp")
	}
	b, err := exec.CommandContext(ctx, "gh", args...).Output()
	if err != nil {
		return nil, errors.New("GitHub data unavailable: check the repository/PR exists, your gh login and repository access, then retry")
	}
	return b, nil
}

type statusUser struct {
	ID int64 `json:"id"`
}
type commitObservation struct {
	ID        int64      `json:"id"`
	Context   string     `json:"context"`
	State     string     `json:"state"`
	TargetURL string     `json:"target_url"`
	CreatedAt time.Time  `json:"created_at"`
	Creator   statusUser `json:"creator"`
}
type statusComment struct {
	Body      string     `json:"body"`
	HTMLURL   string     `json:"html_url"`
	CreatedAt time.Time  `json:"created_at"`
	User      statusUser `json:"user"`
}
type statusPR struct {
	Number   uint64     `json:"number"`
	State    string     `json:"state"`
	Merged   bool       `json:"merged"`
	ClosedAt *time.Time `json:"closed_at"`
	Head     struct {
		SHA string `json:"sha"`
	} `json:"head"`
}

// previewStatus is a read-only snapshot, never a claim of a fresh network probe.
type previewStatus struct {
	Repository   string                `json:"repository"`
	PR           string                `json:"pr"`
	PRState      string                `json:"pr_state"`
	SHA          string                `json:"sha"`
	State        string                `json:"state"`
	Build        string                `json:"build"`
	Deployment   string                `json:"deployment"`
	Readiness    string                `json:"readiness"`
	Health       string                `json:"health"`
	Cleanup      string                `json:"cleanup"`
	URL          string                `json:"url,omitempty"`
	FailedStage  string                `json:"failed_stage,omitempty"`
	EvidenceURL  string                `json:"evidence_url"`
	RunURL       string                `json:"run_url,omitempty"`
	ObservedAt   *time.Time            `json:"observed_at,omitempty"`
	QueriedAt    time.Time             `json:"queried_at"`
	StageTimings []statusrecord.Timing `json:"stage_timings,omitempty"`
	Runtime      map[string]string     `json:"runtime,omitempty"`
	Rollback     string                `json:"rollback,omitempty"`
	Explanation  string                `json:"explanation"`
	NextAction   string                `json:"next_action"`
}

// safeStatusURL excludes credentials and terminal control characters from links.
func safeStatusURL(raw string) bool {
	u, err := url.Parse(raw)
	if err != nil || u.Hostname() == "" || u.User != nil || (u.Scheme != "http" && u.Scheme != "https") {
		return false
	}
	for _, c := range raw {
		if c <= 32 || c == 127 {
			return false
		}
	}
	return true
}

func loadPreviewStatus(read func(string, bool) ([]byte, error), repo, pr string, now time.Time, maxAge time.Duration) (previewStatus, error) {
	s := previewStatus{Repository: repo, PR: pr, State: "missing", Build: "unknown", Deployment: "unknown", Readiness: "unknown", Health: "unknown", Cleanup: "unknown", QueriedAt: now,
		EvidenceURL: "https://github.com/" + repo + "/pull/" + pr,
		Explanation: "No complete PreviewMesh evidence for the current PR revision.", NextAction: "Check the PR feedback and control workflow; trigger a new preview attempt if no run exists."}
	var p statusPR
	b, err := read("repos/"+repo+"/pulls/"+pr, false)
	if err != nil {
		return s, err
	}
	if json.Unmarshal(b, &p) != nil || strconv.FormatUint(p.Number, 10) != pr || !shaPattern.MatchString(p.Head.SHA) || (p.State != "open" && p.State != "closed") {
		return s, errors.New("GitHub returned invalid PR data; check the repository and PR number")
	}
	s.PRState, s.SHA = p.State, p.Head.SHA
	closed := p.State == "closed" || p.Merged
	if p.Merged {
		s.PRState = "merged"
	}
	if closed {
		s.Explanation = "PR is closed; the preview URL is expired. Cleanup has not been confirmed."
		s.NextAction = "Check the cleanup workflow; rerun cleanup if it is missing or failed."
	}
	var statuses [][]commitObservation
	b, err = read("repos/"+repo+"/commits/"+p.Head.SHA+"/statuses?per_page=100", true)
	if err != nil {
		return s, err
	}
	if json.Unmarshal(b, &statuses) != nil {
		return s, errors.New("invalid commit status response; retry GitHub query")
	}
	var latest commitObservation
	for _, page := range statuses {
		for _, item := range page {
			if item.Context == "PreviewMesh" && item.ID > latest.ID {
				latest = item
			}
		}
	}
	var comments [][]statusComment
	b, err = read("repos/"+repo+"/issues/"+pr+"/comments?per_page=100", true)
	if err != nil {
		return s, err
	}
	if json.Unmarshal(b, &comments) != nil {
		return s, errors.New("invalid PR feedback response; retry GitHub query")
	}
	var record *statusrecord.Record
	for _, page := range comments {
		for _, comment := range page {
			start := strings.Index(comment.Body, statusrecord.Marker)
			if start < 0 {
				continue
			}
			data := comment.Body[start+len(statusrecord.Marker):]
			end := strings.Index(data, " -->")
			if end < 0 {
				continue
			}
			var candidate statusrecord.Record
			if json.Unmarshal([]byte(data[:end]), &candidate) != nil || !strings.EqualFold(candidate.Repository, repo) || candidate.PR != pr {
				continue
			}
			// A comment must come from the status publisher and reference its exact ID.
			if latest.ID > 0 && candidate.StatusID == latest.ID && candidate.SHA == p.Head.SHA && candidate.State == latest.State && latest.Creator.ID > 0 && comment.User.ID == latest.Creator.ID {
				record = &candidate
				if safeStatusURL(comment.HTMLURL) {
					s.EvidenceURL = comment.HTMLURL
				}
			} else if latest.ID == 0 && candidate.SHA != p.Head.SHA {
				s.State = "expired"
				s.Explanation = "Only evidence for an older PR revision exists; it cannot establish current preview health."
			}
		}
	}
	if latest.ID == 0 {
		return s, nil
	}
	s.ObservedAt = &latest.CreatedAt
	if safeStatusURL(latest.TargetURL) && strings.HasPrefix(latest.TargetURL, "https://github.com/") {
		s.RunURL = latest.TargetURL
	}
	if latest.State == "pending" {
		s.State = "running"
		s.Explanation = "The latest attempt is pending; stage completion has not been reported."
		s.NextAction = "Open the workflow run to check progress, then query again; rerun if the workflow was cancelled."
		if closed {
			s.Cleanup = "pending"
			s.Explanation = "PR is closed; preview URL expired. Awaiting cleanup confirmation."
		}
	} else if latest.State == "failure" || latest.State == "error" {
		s.State = "failure"
		s.FailedStage = "unreported"
		s.Explanation = "The latest attempt failed; see the workflow evidence."
		s.NextAction = "Open the workflow logs and evidence artifact, fix the failed stage, then rerun the attempt."
	}
	if record != nil {
		r := record
		s.Runtime, s.Rollback = r.Runtime, r.Rollback
		if safeStatusURL(r.RunURL) {
			s.RunURL = r.RunURL
		}
		if r.Build != "" {
			s.Build = r.Build
		}
		if r.Cleanup != "" {
			s.Cleanup = r.Cleanup
		}
		s.StageTimings = r.StageTimings
		for _, timing := range r.StageTimings {
			switch timing.Stage {
			case "deploy":
				s.Deployment = timing.Result
			case "readiness":
				s.Readiness = timing.Result
			}
		}
		if r.HTTPVerification != "" {
			s.Health = fmt.Sprintf("%s (HTTP %d; revision %s)", r.HTTPVerification, r.HTTPStatus, r.RevisionVerification)
		}
		if r.FailedStage != "" {
			s.FailedStage = r.FailedStage
		}
		if s.State == "failure" && s.FailedStage == "unreported" && r.Build == "failure" {
			s.FailedStage = "build"
		}
		if closed {
			// Evidence predating closure cannot prove that closure cleanup completed.
			if r.Cleanup == "confirmed_absent" && (p.ClosedAt == nil || latest.CreatedAt.Before(*p.ClosedAt)) {
				s.Cleanup = "unconfirmed (cleanup evidence predates this closure or closure time is unavailable)"
			}
			if p.ClosedAt != nil && !latest.CreatedAt.Before(*p.ClosedAt) && r.Cleanup == "confirmed_absent" && latest.State == "success" {
				s.State = "removed"
				s.Explanation = "PR is closed and cleanup confirmed the preview is absent."
				s.NextAction = "No action required."
			}
		} else if latest.State == "success" && r.Result == "success" && r.Build == "success" && s.Deployment == "success" && s.Readiness == "success" && r.HTTPStatus == 200 && r.HTTPVerification == "success" && r.RevisionVerification == "success" && r.RequestedSHA == p.Head.SHA && r.ServedSHA == p.Head.SHA && r.FailedStage == "" && safeStatusURL(r.URL) && r.URL == latest.TargetURL {
			s.State = "ready"
			s.URL = r.URL
			s.Explanation = "Current revision passed build, deployment, readiness and health checks at the recorded time; this query does not probe the preview."
			s.NextAction = "Open the preview using the configured preview network and hostname mapping."
		}
	}
	if s.State == "failure" {
		switch s.FailedStage {
		case "build", "build_push":
			s.NextAction = "Inspect the build job and build evidence; fix the Docker build or image publishing failure, then rerun the workflow."
		case "deploy":
			s.NextAction = "Inspect deployment evidence and Helm logs; correct the chart or image access, then rerun the workflow."
		case "readiness":
			s.NextAction = "Inspect pod readiness, image pull errors and Service/Ingress observations in the workflow evidence, then rerun."
		case "http_verify":
			s.NextAction = "Inspect /health status, body and served revision in the workflow evidence; fix application health or routing, then rerun."
		case "cleanup", "resource_verify":
			s.NextAction = "Inspect cleanup evidence for remaining resources; use the documented cleanup inspection and retry procedure."
		}
	}
	// Treat old or undated observations as expired, except confirmed resource removal.
	if s.State != "removed" && (latest.CreatedAt.IsZero() || now.Sub(latest.CreatedAt) > maxAge || latest.CreatedAt.After(now.Add(time.Minute))) {
		s.State = "expired"
		s.URL = ""
		s.Explanation = "Latest evidence is expired or has an invalid timestamp; current health is unknown."
		s.NextAction = "Check the latest workflow and rerun deployment or cleanup to obtain fresh evidence."
	}
	return s, nil
}

// runStatus is deliberately separate from deployment parsing and its trusted registry.
func runStatus(args []string, out io.Writer) error {
	f := flag.NewFlagSet("status", flag.ContinueOnError)
	repo := f.String("repo", "", "source repository owner/name")
	pr := f.String("pr", "", "pull request number")
	asJSON := f.Bool("json", false, "print structured JSON")
	maxAge := f.Duration("max-age", 24*time.Hour, "maximum age of health evidence")
	if err := f.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if f.NArg() != 0 || !sourcePattern.MatchString(*repo) || !decimal.MatchString(*pr) || *maxAge <= 0 {
		return errors.New("usage: previewmesh status --repo owner/name --pr NUMBER [--json] [--max-age 24h]")
	}
	if _, err := strconv.ParseUint(*pr, 10, 64); err != nil {
		return errors.New("PR number is out of range")
	}
	if _, err := exec.LookPath("gh"); err != nil {
		return errors.New("GitHub CLI (gh) is required: install gh and run gh auth login, then retry")
	}
	s, err := loadPreviewStatus(githubRead, *repo, *pr, time.Now().UTC(), *maxAge)
	if err != nil {
		return err
	}
	if *asJSON {
		enc := json.NewEncoder(out)
		enc.SetIndent("", "  ")
		return enc.Encode(s)
	}
	// Quote remotely supplied text so terminal escape sequences stay inert.
	fmt.Fprintf(out, "%s #%s — PR %s\nStatus: %s\nCommit: %s\n", s.Repository, s.PR, s.PRState, s.State, s.SHA)
	for _, row := range [][2]string{{"Build", s.Build}, {"Deployment", s.Deployment}, {"Readiness", s.Readiness}, {"Health", s.Health}, {"Cleanup", s.Cleanup}} {
		fmt.Fprintf(out, "%s: %q\n", row[0], row[1])
	}
	for _, kind := range []string{"deployment", "pods", "service", "ingress"} {
		if value := s.Runtime[kind]; value != "" {
			fmt.Fprintf(out, "Observed %s: %q\n", kind, value)
		}
	}
	if s.Rollback != "" {
		fmt.Fprintf(out, "Rollback: %q\n", s.Rollback)
	}
	if s.URL != "" {
		fmt.Fprintln(out, "Preview URL:", s.URL)
	} else {
		fmt.Fprintln(out, "Preview URL: unavailable / expired")
	}
	if s.FailedStage != "" {
		fmt.Fprintf(out, "Failed stage: %q\n", s.FailedStage)
	}
	fmt.Fprintln(out, "Evidence:", s.EvidenceURL)
	if s.RunURL != "" {
		fmt.Fprintln(out, "Workflow:", s.RunURL)
	}
	if s.ObservedAt != nil {
		fmt.Fprintln(out, "Observed at:", s.ObservedAt.UTC().Format(time.RFC3339))
	} else {
		fmt.Fprintln(out, "Observed at: unavailable")
	}
	for _, timing := range s.StageTimings {
		fmt.Fprintf(out, "Timing %q: %q → %q (%.3fs, %q)\n", timing.Stage, timing.StartedAtUTC, timing.EndedAtUTC, timing.DurationSeconds, timing.Result)
	}
	fmt.Fprintln(out, s.Explanation)
	fmt.Fprintln(out, "Next:", s.NextAction)
	return nil
}
