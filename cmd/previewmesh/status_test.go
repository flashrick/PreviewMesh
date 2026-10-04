package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"testing"
	"time"

	"previewmesh/internal/statusrecord"
)

const (
	statusTestRepo = "owner/demo"
	statusTestPR   = "3"
)

type statusFixtureReader struct {
	responses map[string][]byte
	errors    map[string]error
}

func (r statusFixtureReader) read(endpoint string, _ bool) ([]byte, error) {
	if err := r.errors[endpoint]; err != nil {
		return nil, err
	}
	data, ok := r.responses[endpoint]
	if !ok {
		return nil, fmt.Errorf("unexpected endpoint %q", endpoint)
	}
	return data, nil
}

func statusTestJSON(t *testing.T, value any) []byte {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func statusTestSHA(ch byte) string { return strings.Repeat(string(ch), 40) }

func statusTestPRData(t *testing.T, sha, state string, merged bool, closedAt *time.Time) []byte {
	t.Helper()
	return statusTestJSON(t, map[string]any{
		"number":    3,
		"state":     state,
		"merged":    merged,
		"closed_at": closedAt,
		"head":      map[string]string{"sha": sha},
	})
}

func statusTestReader(t *testing.T, pr []byte, sha string, statuses [][]commitObservation, comments [][]statusComment) statusFixtureReader {
	t.Helper()
	return statusFixtureReader{
		responses: map[string][]byte{
			"repos/" + statusTestRepo + "/pulls/" + statusTestPR:                             pr,
			"repos/" + statusTestRepo + "/commits/" + sha + "/statuses?per_page=100":         statusTestJSON(t, statuses),
			"repos/" + statusTestRepo + "/issues/" + statusTestPR + "/comments?per_page=100": statusTestJSON(t, comments),
		},
		errors: map[string]error{},
	}
}

func statusTestObservation(id int64, state, target string, created time.Time, creatorID int64) commitObservation {
	return commitObservation{ID: id, Context: "PreviewMesh", State: state, TargetURL: target, CreatedAt: created, Creator: statusUser{ID: creatorID}}
}

func statusTestRecord(statusID int64, sha, state, runURL, previewURL string) statusrecord.Record {
	return statusrecord.Record{
		StatusID: statusID, Repository: statusTestRepo, PR: statusTestPR, SHA: sha, State: state,
		RunURL: runURL, URL: previewURL, Build: "success", Result: "success",
		Runtime:    map[string]string{"deployment": "ready 1/1", "pods": "ready 1/1"},
		HTTPStatus: 200, HTTPVerification: "success", RequestedSHA: sha, ServedSHA: sha,
		RevisionVerification: "success", Cleanup: "not_attempted",
		StageTimings: []statusrecord.Timing{
			{Stage: "deploy", StartedAtUTC: "2026-10-04T00:00:00Z", EndedAtUTC: "2026-10-04T00:00:01Z", DurationSeconds: 1, Result: "success"},
			{Stage: "readiness", StartedAtUTC: "2026-10-04T00:00:01Z", EndedAtUTC: "2026-10-04T00:00:03Z", DurationSeconds: 2, Result: "success"},
		},
	}
}

func statusTestComment(t *testing.T, record statusrecord.Record, userID int64, htmlURL string) statusComment {
	t.Helper()
	data := statusTestJSON(t, record)
	return statusComment{
		Body:      "### PreviewMesh deployment\n" + statusrecord.Marker + string(data) + " -->\n",
		HTMLURL:   htmlURL,
		CreatedAt: time.Date(2026, 10, 4, 0, 0, 10, 0, time.UTC),
		User:      statusUser{ID: userID},
	}
}

func TestLoadPreviewStatusReady(t *testing.T) {
	now := time.Date(2026, 10, 4, 1, 0, 0, 0, time.UTC)
	created := now.Add(-5 * time.Minute)
	sha := statusTestSHA('a')
	runURL := "https://github.com/owner/control/actions/runs/42"
	previewURL := "http://pm-r12-pr3.preview.test:18080"
	record := statusTestRecord(42, sha, "success", runURL, previewURL)
	reader := statusTestReader(t, statusTestPRData(t, sha, "open", false, nil), sha,
		[][]commitObservation{{statusTestObservation(42, "success", previewURL, created, 7)}},
		[][]statusComment{{statusTestComment(t, record, 7, "https://github.com/owner/demo/pull/3#issuecomment-42")}})

	got, err := loadPreviewStatus(reader.read, statusTestRepo, statusTestPR, now, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	if got.State != "ready" || got.PRState != "open" || got.SHA != sha || got.URL != previewURL {
		t.Fatalf("ready status = %+v", got)
	}
	if got.Build != "success" || got.Deployment != "success" || got.Readiness != "success" || got.Health != "success (HTTP 200; revision success)" {
		t.Fatalf("incomplete ready evidence = %+v", got)
	}
	if got.RunURL != runURL || !strings.Contains(got.EvidenceURL, "issuecomment-42") || got.ObservedAt == nil || !got.ObservedAt.Equal(created) {
		t.Fatalf("links/timing = %+v", got)
	}
	for _, want := range []string{"sslip.io", "no hosts edits", "manual DNS/hosts", "ops/install/access.md"} {
		if !strings.Contains(got.NextAction, want) {
			t.Errorf("ready access guidance missing %q: %q", want, got.NextAction)
		}
	}
	if strings.Contains(got.NextAction, "hostname mapping") {
		t.Errorf("ready guidance retains stale hostname mapping wording: %q", got.NextAction)
	}
	if len(got.StageTimings) != 2 || got.StageTimings[1].Stage != "readiness" {
		t.Fatalf("stage timings = %+v", got.StageTimings)
	}
}

func TestLoadPreviewStatusFailureIncludesEvidence(t *testing.T) {
	now := time.Date(2026, 10, 4, 1, 0, 0, 0, time.UTC)
	sha := statusTestSHA('b')
	runURL := "https://github.com/owner/control/actions/runs/43"
	record := statusTestRecord(43, sha, "failure", runURL, "")
	record.Result = "failure"
	record.Build = "success"
	record.FailedStage = "readiness"
	record.HTTPStatus = 503
	record.HTTPVerification = "failure"
	record.RevisionVerification = "not_checked"
	record.StageTimings[1].Result = "failure"
	reader := statusTestReader(t, statusTestPRData(t, sha, "open", false, nil), sha,
		[][]commitObservation{{statusTestObservation(43, "failure", runURL, now.Add(-time.Minute), 7)}},
		[][]statusComment{{statusTestComment(t, record, 7, "https://github.com/owner/demo/pull/3#issuecomment-43")}})

	got, err := loadPreviewStatus(reader.read, statusTestRepo, statusTestPR, now, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	if got.State != "failure" || got.FailedStage != "readiness" || got.URL != "" {
		t.Fatalf("failure status = %+v", got)
	}
	if got.Build != "success" || got.Deployment != "success" || got.Readiness != "failure" || got.Health != "failure (HTTP 503; revision not_checked)" {
		t.Fatalf("failure evidence = %+v", got)
	}
	if got.NextAction == "" || !strings.Contains(got.Explanation, "failed") {
		t.Fatalf("failure guidance = %+v", got)
	}
}

func TestLoadPreviewStatusMissing(t *testing.T) {
	now := time.Date(2026, 10, 4, 1, 0, 0, 0, time.UTC)
	sha := statusTestSHA('c')
	reader := statusTestReader(t, statusTestPRData(t, sha, "open", false, nil), sha,
		[][]commitObservation{{}}, [][]statusComment{{}})

	got, err := loadPreviewStatus(reader.read, statusTestRepo, statusTestPR, now, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	if got.State != "missing" || got.PRState != "open" || got.URL != "" {
		t.Fatalf("missing status = %+v", got)
	}
	for name, value := range map[string]string{"build": got.Build, "deployment": got.Deployment, "readiness": got.Readiness, "health": got.Health, "cleanup": got.Cleanup} {
		if value != "unknown" {
			t.Errorf("missing %s = %q, want unknown", name, value)
		}
	}
	if !strings.Contains(got.Explanation, "No complete") || got.NextAction == "" {
		t.Fatalf("missing guidance = %+v", got)
	}
}

func TestLoadPreviewStatusOldSHAIsExpired(t *testing.T) {
	now := time.Date(2026, 10, 4, 1, 0, 0, 0, time.UTC)
	currentSHA := statusTestSHA('d')
	oldSHA := statusTestSHA('e')
	old := statusTestRecord(44, oldSHA, "success", "https://github.com/owner/control/actions/runs/44", "http://pm-r12-pr3.preview.test:18080")
	reader := statusTestReader(t, statusTestPRData(t, currentSHA, "open", false, nil), currentSHA,
		[][]commitObservation{{}}, [][]statusComment{{statusTestComment(t, old, 7, "https://github.com/owner/demo/pull/3#issuecomment-44")}})

	got, err := loadPreviewStatus(reader.read, statusTestRepo, statusTestPR, now, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	if got.State != "expired" || got.SHA != currentSHA || got.URL != "" {
		t.Fatalf("old SHA status = %+v", got)
	}
	if !strings.Contains(got.Explanation, "older PR revision") {
		t.Fatalf("old SHA explanation = %q", got.Explanation)
	}
}

func TestLoadPreviewStatusPendingIsRunning(t *testing.T) {
	now := time.Date(2026, 10, 4, 1, 0, 0, 0, time.UTC)
	sha := statusTestSHA('f')
	runURL := "https://github.com/owner/control/actions/runs/45"
	reader := statusTestReader(t, statusTestPRData(t, sha, "open", false, nil), sha,
		[][]commitObservation{{statusTestObservation(45, "pending", runURL, now.Add(-time.Minute), 7)}},
		[][]statusComment{{}})

	got, err := loadPreviewStatus(reader.read, statusTestRepo, statusTestPR, now, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	if got.State != "running" || got.RunURL != runURL || got.URL != "" {
		t.Fatalf("pending status = %+v", got)
	}
	if !strings.Contains(got.Explanation, "pending") || got.NextAction == "" {
		t.Fatalf("pending guidance = %+v", got)
	}
}

func TestLoadPreviewStatusExpiredEvidenceCannotAdvertiseURL(t *testing.T) {
	now := time.Date(2026, 10, 4, 3, 0, 0, 0, time.UTC)
	sha := statusTestSHA('a')
	previewURL := "http://pm-r12-pr3.preview.test:18080"
	record := statusTestRecord(46, sha, "success", "https://github.com/owner/control/actions/runs/46", previewURL)
	created := now.Add(-2 * time.Hour)
	reader := statusTestReader(t, statusTestPRData(t, sha, "open", false, nil), sha,
		[][]commitObservation{{statusTestObservation(46, "success", previewURL, created, 7)}},
		[][]statusComment{{statusTestComment(t, record, 7, "https://github.com/owner/demo/pull/3#issuecomment-46")}})

	got, err := loadPreviewStatus(reader.read, statusTestRepo, statusTestPR, now, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	if got.State != "expired" || got.URL != "" {
		t.Fatalf("expired status = %+v", got)
	}
	if !strings.Contains(got.Explanation, "expired") || got.NextAction == "" {
		t.Fatalf("expired guidance = %+v", got)
	}
}

func TestLoadPreviewStatusClosedAndMergedCleanup(t *testing.T) {
	now := time.Date(2026, 10, 4, 1, 0, 0, 0, time.UTC)
	closedAt := now.Add(-10 * time.Minute)
	for _, tc := range []struct {
		name   string
		merged bool
	}{
		{name: "closed", merged: false},
		{name: "merged", merged: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			sha := statusTestSHA('a')
			runURL := "https://github.com/owner/control/actions/runs/" + tc.name
			record := statusTestRecord(47, sha, "success", runURL, "")
			record.Cleanup = "confirmed_absent"
			record.URL = ""
			record.StageTimings = []statusrecord.Timing{{Stage: "cleanup", StartedAtUTC: "2026-10-04T00:55:00Z", EndedAtUTC: "2026-10-04T00:56:00Z", DurationSeconds: 60, Result: "success"}}
			reader := statusTestReader(t, statusTestPRData(t, sha, "closed", tc.merged, &closedAt), sha,
				[][]commitObservation{{statusTestObservation(47, "success", runURL, now.Add(-5*time.Minute), 7)}},
				[][]statusComment{{statusTestComment(t, record, 7, "https://github.com/owner/demo/pull/3#issuecomment-47")}})

			got, err := loadPreviewStatus(reader.read, statusTestRepo, statusTestPR, now, time.Hour)
			if err != nil {
				t.Fatal(err)
			}
			wantPRState := "closed"
			if tc.merged {
				wantPRState = "merged"
			}
			if got.State != "removed" || got.PRState != wantPRState || got.Cleanup != "confirmed_absent" || got.URL != "" {
				t.Fatalf("cleanup status = %+v", got)
			}
			if got.NextAction != "No action required." || !strings.Contains(got.Explanation, "cleanup confirmed") {
				t.Fatalf("cleanup guidance = %+v", got)
			}
		})
	}
}

func TestLoadPreviewStatusClosedCleanupPending(t *testing.T) {
	now := time.Date(2026, 10, 4, 1, 0, 0, 0, time.UTC)
	sha := statusTestSHA('b')
	runURL := "https://github.com/owner/control/actions/runs/48"
	reader := statusTestReader(t, statusTestPRData(t, sha, "closed", false, nil), sha,
		[][]commitObservation{{statusTestObservation(48, "pending", runURL, now.Add(-time.Minute), 7)}},
		[][]statusComment{{}})

	got, err := loadPreviewStatus(reader.read, statusTestRepo, statusTestPR, now, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	if got.State != "running" || got.Cleanup != "pending" || got.URL != "" {
		t.Fatalf("pending cleanup = %+v", got)
	}
	if !strings.Contains(got.Explanation, "Awaiting cleanup") {
		t.Fatalf("pending cleanup explanation = %q", got.Explanation)
	}
}

func TestLoadPreviewStatusRejectsGitHubErrorsAndMalformedData(t *testing.T) {
	now := time.Date(2026, 10, 4, 1, 0, 0, 0, time.UTC)
	sha := statusTestSHA('a')
	prEndpoint := "repos/" + statusTestRepo + "/pulls/" + statusTestPR
	statusEndpoint := "repos/" + statusTestRepo + "/commits/" + sha + "/statuses?per_page=100"
	commentEndpoint := "repos/" + statusTestRepo + "/issues/" + statusTestPR + "/comments?per_page=100"
	for _, tc := range []struct {
		name     string
		endpoint string
		data     []byte
		want     string
	}{
		{name: "API error", endpoint: prEndpoint},
		{name: "invalid PR", endpoint: prEndpoint, data: []byte(`{"number":3,"state":"open","head":{"sha":"bad"}}`), want: "invalid PR data"},
		{name: "invalid statuses", endpoint: statusEndpoint, data: []byte(`not-json`), want: "invalid commit status"},
		{name: "invalid comments", endpoint: commentEndpoint, data: []byte(`not-json`), want: "invalid PR feedback"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			reader := statusTestReader(t, statusTestPRData(t, sha, "open", false, nil), sha, [][]commitObservation{{}}, [][]statusComment{{}})
			if tc.data != nil {
				reader.responses[tc.endpoint] = tc.data
			} else {
				reader.errors[tc.endpoint] = errors.New("request failed")
			}
			_, err := loadPreviewStatus(reader.read, statusTestRepo, statusTestPR, now, time.Hour)
			if err == nil || (tc.want != "" && !strings.Contains(err.Error(), tc.want)) {
				t.Fatalf("error = %v, want substring %q", err, tc.want)
			}
		})
	}
}

func TestLoadPreviewStatusRejectsWrongAssociationAndAuthor(t *testing.T) {
	now := time.Date(2026, 10, 4, 1, 0, 0, 0, time.UTC)
	sha := statusTestSHA('a')
	target := "http://pm-r12-pr3.preview.test:18080"
	latest := statusTestObservation(49, "success", target, now.Add(-time.Minute), 7)
	for _, tc := range []struct {
		name       string
		mutate     func(statusrecord.Record) statusrecord.Record
		commentUID int64
	}{
		{name: "wrong status association", mutate: func(r statusrecord.Record) statusrecord.Record { r.StatusID = 999; return r }, commentUID: 7},
		{name: "unauthorized comment author", mutate: func(r statusrecord.Record) statusrecord.Record { return r }, commentUID: 8},
		{name: "wrong repository", mutate: func(r statusrecord.Record) statusrecord.Record { r.Repository = "other/demo"; return r }, commentUID: 7},
		{name: "wrong pull request", mutate: func(r statusrecord.Record) statusrecord.Record { r.PR = "4"; return r }, commentUID: 7},
	} {
		t.Run(tc.name, func(t *testing.T) {
			record := tc.mutate(statusTestRecord(49, sha, "success", "https://github.com/owner/control/actions/runs/49", target))
			reader := statusTestReader(t, statusTestPRData(t, sha, "open", false, nil), sha,
				[][]commitObservation{{latest}},
				[][]statusComment{{statusTestComment(t, record, tc.commentUID, "https://example.com/attacker")}})

			got, err := loadPreviewStatus(reader.read, statusTestRepo, statusTestPR, now, time.Hour)
			if err != nil {
				t.Fatal(err)
			}
			if got.State == "ready" || got.URL != "" || got.EvidenceURL != "https://github.com/"+statusTestRepo+"/pull/"+statusTestPR {
				t.Fatalf("untrusted evidence was accepted: %+v", got)
			}
		})
	}
}

func TestLoadPreviewStatusReadsPaginatedStatusesAndComments(t *testing.T) {
	now := time.Date(2026, 10, 4, 1, 0, 0, 0, time.UTC)
	sha := statusTestSHA('a')
	previewURL := "http://pm-r12-pr3.preview.test:18080"
	runURL := "https://github.com/owner/control/actions/runs/50"
	record := statusTestRecord(50, sha, "success", runURL, previewURL)
	reader := statusTestReader(t, statusTestPRData(t, sha, "open", false, nil), sha,
		[][]commitObservation{
			{statusTestObservation(12, "failure", runURL, now.Add(-10*time.Minute), 7)},
			{statusTestObservation(50, "success", previewURL, now.Add(-time.Minute), 7)},
		},
		[][]statusComment{
			{{Body: "unrelated comment", User: statusUser{ID: 7}}},
			{statusTestComment(t, record, 7, "https://github.com/owner/demo/pull/3#issuecomment-50")},
		})

	got, err := loadPreviewStatus(reader.read, statusTestRepo, statusTestPR, now, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	if got.State != "ready" || got.URL != previewURL || got.RunURL != runURL || got.EvidenceURL == "" {
		t.Fatalf("paginated status = %+v", got)
	}
}

func TestRunStatusRejectsInvalidArguments(t *testing.T) {
	for _, args := range [][]string{
		{"--repo", "invalid", "--pr", "3"},
		{"--repo", statusTestRepo, "--pr", "0"},
		{"--repo", statusTestRepo, "--pr", "03"},
		{"--repo", statusTestRepo, "--pr", "3", "--max-age", "0s"},
	} {
		if err := runStatus(args, io.Discard); err == nil {
			t.Errorf("accepted invalid arguments: %v", args)
		}
	}
}
