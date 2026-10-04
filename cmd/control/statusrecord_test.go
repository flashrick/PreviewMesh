package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"previewmesh/internal/statusrecord"
)

func TestReportStatusPublishesStructuredEvidence(t *testing.T) {
	sha := strings.Repeat("a", 40)
	runURL := "https://github.com/owner/control/actions/runs/51"
	previewURL := "http://pm-r12-pr3.preview.test:18080"
	details := reportEvidence{
		Build:                "success",
		Runtime:              map[string]string{"deployment": "ready 1/1", "pods": "ready 1/1"},
		HTTPStatus:           200,
		HTTPVerification:     "success",
		RequestedSHA:         sha,
		ServedSHA:            sha,
		RevisionVerification: "success",
		Result:               "success",
		StageTimings: []statusrecord.Timing{{
			Stage: "readiness", StartedAtUTC: "2026-10-04T00:00:00Z", EndedAtUTC: "2026-10-04T00:00:01Z", DurationSeconds: 1, Result: "success",
		}},
	}
	var commentBody string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.Method != http.MethodPost || req.Header.Get("Authorization") != "Bearer test" {
			t.Errorf("unexpected request: %s %s", req.Method, req.URL.Path)
		}
		var payload map[string]any
		if err := json.NewDecoder(req.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		switch req.URL.Path {
		case "/repos/owner/demo/statuses/" + sha:
			if payload["context"] != "PreviewMesh" || payload["state"] != "success" {
				t.Errorf("status payload = %v", payload)
			}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusCreated)
			fmt.Fprint(w, `{"id":51}`)
		case "/repos/owner/demo/issues/3/comments":
			var ok bool
			commentBody, ok = payload["body"].(string)
			if !ok {
				t.Fatal("comment body is not a string")
			}
			w.WriteHeader(http.StatusCreated)
		default:
			t.Errorf("unexpected path: %s", req.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer srv.Close()

	if err := reportStatus(api{base: srv.URL, token: "test", client: srv.Client()}, registration{Source: "owner/demo"}, "3", sha, "success", "Preview ready", previewURL, runURL, true, details); err != nil {
		t.Fatal(err)
	}
	start := strings.Index(commentBody, statusrecord.Marker)
	if start < 0 {
		t.Fatalf("structured evidence marker missing: %q", commentBody)
	}
	data := commentBody[start+len(statusrecord.Marker):]
	end := strings.Index(data, " -->")
	if end < 0 {
		t.Fatalf("structured evidence terminator missing: %q", commentBody)
	}
	var record statusrecord.Record
	if err := json.Unmarshal([]byte(data[:end]), &record); err != nil {
		t.Fatalf("structured evidence JSON: %v", err)
	}
	if strings.Contains(data[:end], "error") || strings.Contains(data[:end], "secret") {
		t.Fatalf("structured evidence contains a non-allowlisted field: %s", data[:end])
	}
	if record.StatusID != 51 || record.Repository != "owner/demo" || record.PR != "3" || record.SHA != sha || record.State != "success" || record.RunURL != runURL || record.URL != previewURL {
		t.Fatalf("identity evidence = %+v", record)
	}
	if record.Build != details.Build || record.Result != details.Result || record.HTTPStatus != details.HTTPStatus || record.HTTPVerification != details.HTTPVerification || len(record.StageTimings) != 1 || record.StageTimings[0] != details.StageTimings[0] {
		t.Fatalf("result evidence = %+v", record)
	}
}
